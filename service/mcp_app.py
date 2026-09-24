"""FastMCP thin wrapper over service.core (see docs/architecture.md).

Tool descriptions are a shipped UX surface.
Each docstring includes a "use when" and "do NOT use when" so an LLM-side
MCP client picks the right tool for the right reason.
"""
from __future__ import annotations

import functools
import json
import os
import time
from typing import Any, Literal

import anyio
import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from . import __version__, auth, core, optix_schema

# Shared MCP ToolAnnotations: 3 distinct hint tuples cover all tools.
# Safe to share single instances across registrations -- FastMCP only
# reads them (Tool.from_function stores the reference and model_dump()s
# it for tool listing); nothing mutates a ToolAnnotations post-build.
_RO = ToolAnnotations(readOnlyHint=True)
_RW = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_RW_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

# The low-level server sets this contextvar for the duration of each request;
# for the streamable-HTTP transport its `.request` is the Starlette Request
# whose `.scope` is the ASGI scope that AuthMiddleware augments with
# `ftxm.token_scope`. Typed Any so the None fallback is a valid assignment.
_mcp_request_ctx: Any
try:
    from mcp.server.lowlevel.server import request_ctx as _mcp_request_ctx
except Exception:  # pragma: no cover - SDK shape guard
    _mcp_request_ctx = None


class ScopeInsufficient(Exception):
    """A token-authenticated MCP call lacked the scope its tool requires."""


def _authenticated_token_scope() -> str | None:
    """Scope of the bearer token that authenticated the current MCP request,
    or None when the request was not token-authenticated (the auth-off loopback
    default, or a non-HTTP transport) or the scope cannot be resolved.

    AuthMiddleware forwards `ftxm.token_scope` on the ASGI scope after a
    successful auth (service/auth.py). Fully guarded: a shape change in the SDK
    must never crash tool dispatch — it degrades to "no enforcement", i.e. the
    pre-existing behavior, never to a crash.
    """
    if _mcp_request_ctx is None:
        return None
    try:
        rc = _mcp_request_ctx.get()
    except LookupError:
        return None
    req = getattr(rc, "request", None)
    scope = getattr(req, "scope", None)
    if not isinstance(scope, dict):
        return None
    val = scope.get("ftxm.token_scope")
    return val if isinstance(val, str) else None


def _required_tool_scope(mcp: FastMCP, name: str) -> str:
    """Minimum token scope to invoke tool `name`, from the explicit
    `auth.TOOL_SCOPES` table (health/read/author/deploy). The table — not the
    binary readOnlyHint/destructiveHint annotations — is authoritative: the
    author/deploy cut is orthogonal to the hints (e.g. optix_runtime_start is
    destructiveHint=False yet deploy; optix_bridge_delete_node is
    destructiveHint=True yet author), so no annotation derivation can produce
    it. An unknown / unclassified tool fails closed to `deploy` (most
    restrictive), matching resolve_required_scope's fallback. `mcp` is retained
    in the signature for the call site + tests; the lookup is name-keyed."""
    return auth.TOOL_SCOPES.get(name, "deploy")


def make_mcp(cfg: core.Config) -> FastMCP:
    mcp = FastMCP(
        "ftx-mcp",
        # Surfaced automatically to the assistant at connect time (the MCP
        # `instructions` field) — the always-visible orientation; full
        # playbooks load on demand via the skill tools. Keep this SHORT: it
        # lands in every connected session's context.
        instructions=(
            "ftx-mcp drives FactoryTalk Optix Studio: author HMI changes into "
            "the OPEN Studio project via the design-time bridge, preview in the "
            "emulator, verify on the canvas.\n"
            "Required first step: optix_get_project_map() -- you are blind to "
            "its screens/variables/structure until you call it. "
            "Everything else is on demand.\n"
            "The tools are self-describing -- author directly, let them correct "
            "you, don't pre-read: optix_describe_type(<type>) lists settable "
            "properties; the validator rejects bad ops with did_you_mean. "
            "optix_list_skills()/optix_get_skill(name) cover the FEW "
            "non-obvious behaviors (no dock-panel, silent-no-op expressions) "
            "-- pull one only when a task hits that case, not up front.\n"
            "Authoring loop: author each component with its own optix_bridge_edit "
            "(validates+applies; component-sized, not whole-screen). Structural edits "
            "render only after a restart, so after ALL components land do ONE "
            "optix_emulator(action='restart') + ONE optix_observe(mode='screenshot') "
            "to verify -- NOT one per component (the top cost sink). "
            "optix_status(action='doctor') if broken.\n"
            "FILES: service filesystem is unreachable -- no host folder "
            "access; use optix_routes(action='save'), "
            "return_image=true, sweep/diff text."
        ),
    )
    # FastMCP doesn't expose a version kwarg; set it on the underlying
    # low-level Server so MCP `initialize` reports our package version
    # instead of the FastMCP library version.
    mcp._mcp_server.version = __version__

    def _resolve_project(project: str | None) -> str | None:
        """Effective project: the explicit arg wins, else the bridge's served
        project. Most calls target the project open in
        Studio, so `project` is optional on every project-scoped tool; a name
        only needs passing (or discovering via optix_list_projects) when
        working on a DIFFERENT project than the one the bridge serves."""
        return project or core.default_project(cfg)

    _NO_PROJECT = {
        "error": "no_project",
        "message": ("no project given and no bridge serving one — pass "
                    "project=, or open the project in Studio and start the "
                    "bridge; optix_list_projects can discover names"),
    }

    def _with_project(fn):
        """Resolve the effective project (explicit arg else bridge default),
        short-circuiting with _NO_PROJECT when none is available, so tool
        bodies can assume `project` is a resolved non-empty name.

        functools.wraps is LOAD-BEARING: FastMCP's Tool.from_function reads
        fn.__name__ (tool name), fn.__doc__ (description) and
        inspect.signature(fn, eval_str=True) (input schema) off whatever
        @mcp.tool receives. wraps copies __name__/__doc__/__annotations__ and
        sets __wrapped__ -> fn, so inspect.signature unwraps to the ORIGINAL
        tool fn and the JSON schema (incl. the `project` field and every other
        param) is generated from the real signature. The wrapper MUST stay a
        plain `def` (never async): the post-registration offload pass skips any
        tool whose is_async is already True, so an async wrapper would silently
        defeat the loop-offload for the project-scoped shell-out tools
        (optix_cdp_navigate/sweep/diff/read_text/find_text).
        """
        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            project = _resolve_project(kwargs.get("project"))
            if not project:
                return _NO_PROJECT
            kwargs["project"] = project
            return fn(*args, **kwargs)
        return _wrapper

    # ---- consolidated status surface -------------------------------------
    # optix_status collapses optix_health / optix_doctor / optix_services_status
    # / optix_studio_version into one action-discriminated tool (same rationale
    # as optix_schema/optix_routes/optix_emulator above). The four are
    # HETEROGENEOUS in purpose (fast preflight config vs setup-fix checklist vs
    # live dashboard vs raw binary version) — the docstring makes each action's
    # distinct role explicit rather than flattening them into one concept. All
    # four were already _RO, so the merge is annotation-uniform; no semantic
    # split to reconcile there. Scope: optix_health/optix_services_status were
    # "health" tier, optix_doctor/optix_studio_version were "read" tier; the
    # merged tool takes "read" (most-privileged of the four), the same
    # tightening the optix_routes merge applied to its get/list actions.
    # doctor/services_status shell out -> stays in _OFFLOAD_TOOLS. Low-traffic
    # internal plumbing -> CLEAN REPLACE, no deprecated aliases.
    _STATUS_ACTIONS = ("health", "doctor", "services", "version")

    @mcp.tool(annotations=_RO, name="optix_status")
    def _optix_status_tool(
        action: Literal["health", "doctor", "services", "version"],
        project: str | None = None,
    ) -> dict:
        """Deploy-stack status family — ONE tool, pick an `action`. Consolidates
        optix_health / optix_doctor / optix_services_status / optix_studio_version
        (each formerly its own tool); replaced cleanly, no deprecated aliases.

        These four are HETEROGENEOUS — pick the one that matches what you
        actually need, they are not interchangeable:

        action:
          - "health" — FAST preflight config snapshot (export-based, v0.2.x):
            projects_root, studio_exe, runtime_dir, interactive_session, bind
            config. Use this when the user asks "is everything wired up?",
            "is studio installed?", or before a deploy attempt to fail fast on
            missing config.
          - "doctor" — setup-FIX checklist: every prerequisite plus a plain-
            English fix for each. `ready` is True when the REQUIRED deps
            (Studio, projects folder) are present; feature checks (bridge, cdp,
            deploy creds, interactive session) report their own fix and gate
            only their own feature. Use this for first-time setup, after a
            reboot/config change, or when a tool failed and you want to know
            which dependency is missing. Pass `project` to add a row saying
            WHERE that project name resolved and which source answered —
            "projects_root", or "studio_process" for a project that lives
            outside the projects root and was matched to a running Studio.
            Ignored by the other actions.
          - "services" — LIVE dashboard aggregate: health + studio version +
            runtime/cdp probes in one call — the HMI status-tile payload. Use
            this for rendering an operator dashboard's services panel, or when
            the user asks "what's the state of the deploy stack right now?"
          - "version" — raw `FTOptixStudio.exe --version` output. Use this when
            debugging a deploy failure and you want to confirm the binary
            works, or the user asks which Studio version is installed.

        An unknown `action` returns a structured error rather than raising.

        Use this when:
          - you need ANY status/health/version read on the deploy stack — pick
            the action matching the question (see above); action="doctor" is
            the right default when you're not sure which one you need

        Do NOT use this when:
          - you want live-model project structure (optix_get_project_map /
            optix_describe_node) — this family covers the SERVICE/Studio/
            runtime stack, not the authored HMI model
          - you want a specific runtime slot's liveness (optix_runtime_status)
          - you want the last-deploy outcome details (read
            /services/last-deploy-tail directly; not surfaced as an MCP tool)
        """
        if action not in _STATUS_ACTIONS:
            return {
                "error": "bad_action",
                "message": (f"unknown action {action!r}; valid actions: "
                            f"{', '.join(_STATUS_ACTIONS)}"),
                "valid_actions": list(_STATUS_ACTIONS),
            }
        if action == "health":
            return core.health(cfg)
        if action == "doctor":
            return core.doctor(cfg, project=project)
        if action == "services":
            return core.services_status(cfg)
        # action == "version"
        return core.studio_version(cfg)

    @mcp.tool(annotations=_RO)
    def optix_list_skills() -> dict:
        """Catalog of the bundled authoring playbooks — one line each (proven
        recipes for navigation, bound controls, styles, expressions, alarms).
        Scan for a matching pattern, then optix_get_skill(name) for just that one.

        Use this when:
          - starting a task that smells like a common HMI pattern

        Do NOT use this when:
          - you already know the skill name (optix_get_skill directly)
        """
        return core.list_skills(cfg)

    @mcp.tool(annotations=_RO)
    def optix_get_skill(name: str) -> dict:
        """Full content of one bundled playbook by name (from optix_list_skills).

        Use this when:
          - the task matches a playbook from the catalog

        Do NOT use this when:
          - the task is simple enough that the tool docstrings already cover it
        """
        return core.get_skill(cfg, name)

    @mcp.tool(annotations=_RO)
    def optix_list_projects() -> dict:
        """List Optix projects under OPTIX_PROJECTS_ROOT. You RARELY need this:
        every project-scoped tool defaults to the project open in Studio when
        `project` is omitted.

        Use this when:
          - the user asks "what projects are on the box?"
          - targeting a DIFFERENT project than the one open in Studio
          - no bridge is running and a tool returned no_project

        Do NOT use this when:
          - you're working with the project open in Studio — just omit
            `project` on the other tools; do not enumerate first
        """
        return {"projects": core.list_projects(cfg)}

    @mcp.tool(annotations=_RO)
    def optix_build_check(project: str | None = None) -> dict:
        """Compile the project's NetSolution C# and report errors — a safe
        pre-flight that never touches Studio's build or runs the emulator.

        WHY: a NetLogic that does not compile fails the build silently and takes
        the in-Studio bridge AND the emulator down with it, with no clear signal.
        This copies the NetSolution to a throwaway temp dir (bin/obj excluded) and
        builds the COPY there, so it never touches the project's own bin/obj — a
        concurrent Studio build cannot race it. Returns {ok, returncode,
        error_count, warning_count, errors:[{file,line,col,code,message}],
        warnings, csproj, dotnet}, plus `hint` when the failure looks like stale
        .references and `note_multiple_csproj` when more than one csproj is found.
        Paths are relative to the project root. Unlike optix_find it works whether
        or not Studio is open (it reads the .cs on disk, which is always current —
        only the model YAML lives in Studio's RAM).

        READ THE `hint`: if every error is CS0246 on FTOptix/UAManagedCore types,
        the project's .references HintPaths are stale (pinned to a Studio version
        not installed, or moved between machines) — it builds fine in Studio; do
        NOT report it as a code error. References must resolve from within the
        NetSolution (true for a standard Optix project).

        Use this when:
          - you just authored/edited a .cs (esp. before
            optix_emulator(action="restart")) and want to catch a compile
            error without a full emulator cycle
          - gating a deploy: never ship a NetSolution that does not compile
          - a restart_emulator produced no visible change / the bridge died after
            a rebuild — check for a compile error first

        Do NOT use this when:
          - you changed only model/YAML or widgets (no C#) — nothing to compile
          - the .NET SDK is unavailable (returns {error:"no_dotnet"}; install it)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.build_check(cfg, project)

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_find(
        query: str,
        glob: str = "**/*",
        max_results: int = 200,
        context_lines: int = 2,
        case_sensitive: bool = False,
        project: str | None = None,
    ) -> dict:
        """Search a project's text files for a literal string — server-side.

        THE discovery primitive: locate which file and line hold a screen,
        widget, node name, or property before reading or editing. Case-
        insensitive by default. Literal only — no regex, single-line queries.
        Refuses with `studio_open` (409) while Studio is running (disk state
        is stale while Studio holds the project in memory; close Studio, no
        override exists).

        Use this when:
          - you need to find which Nodes/*.yaml defines a screen or widget
            (e.g. query="Name: Screen1" or query="Type: Label")
          - you want the line number to anchor a ranged optix_read_file

        Do NOT use this when:
          - you already know the file AND region (ranged optix_read_file)
          - you need multi-line matching (read the file instead)
        """
        return core.find_in_project(
            cfg,
            project,
            query,
            glob=glob,
            max_results=max_results,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_read_file(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        project: str | None = None,
    ) -> dict:
        """Read a UTF-8 file under an Optix project, optionally a line range.

        Path-traversal is rejected. start_line/end_line are 1-based inclusive
        (end clamps to EOF). The result's `sha256` is the version fingerprint
        to cite when composing anchored edits.

        Refuses with `studio_open` (409) while Studio is running anywhere on
        this box: disk state is stale while Studio holds the project in
        memory, and an edit planned from it would be wrong. Close Studio and
        retry; no override parameter. (Operators may set
        `OPTIX_STUDIO_GUARD_MODE=attributed` out-of-band to allow reads when
        the bridge proves Studio is serving a DIFFERENT project.)

        Use this when:
          - you need the current content of a YAML/screen file before editing
          - optix_find located the region and you want just that slice
            instead of a 2,000-line file

        Do NOT use this when:
          - you don't know which file holds the node (optix_find first)
          - the file is binary, or you want to list a directory (NOT supported)
        """
        return core.read_file(
            cfg, project, path, start_line=start_line, end_line=end_line
        )

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_read_netlogic(
        cls: str,
        start_line: int | None = None,
        end_line: int | None = None,
        project: str | None = None,
    ) -> dict:
        """Read a NetLogic C# source file by class name from ProjectFiles/NetSolution.

        ``cls`` is the NetLogic class name / file base name WITHOUT the .cs
        extension (e.g. ``"AlarmManager"`` reads ``AlarmManager.cs``).
        Path-traversal and malformed class names are rejected before the
        filesystem is touched. start_line/end_line are 1-based inclusive
        (end clamps to EOF). The result's ``sha256`` is the whole-file
        version fingerprint; ``size`` and ``total_lines`` also describe the
        whole file even when a range is returned.

        Unlike ``optix_read_file`` this tool DOES work while Studio is open:
        the NetSolution lives on disk as a plain C# project — Studio does not
        hold .cs files in its in-memory model. The VS / VS Code attributed-
        editor guard still applies (an unsaved editor buffer would be stale).

        Use this when:
          - you need to read the source of a NetLogic class by name and Studio
            may be open (the normal authoring state)
          - you want a ranged slice of a large .cs file to anchor a targeted
            edit

        Do NOT use this when:
          - you need a Nodes/*.yaml screen or widget definition — use
            optix_read_file for that (it intentionally refuses while Studio is
            open because YAML state IS stale while Studio holds the project)
          - you want to list the NetLogic classes available — use optix_find
            or optix_get_project_map instead
        """
        return core.read_netlogic(
            cfg, project, cls, start_line=start_line, end_line=end_line
        )

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    @_with_project
    def optix_deploy(
        edits: list[dict],
        commit_message: str = "Automated edit",
        run_after_deploy: bool = True,
        project: str | None = None,
    ) -> dict:
        """Apply edits and deploy via FTOptixStudio export -> tree swap -> runtime bounce (v0.2.x).

        *** REQUIRES FACTORYTALK OPTIX STUDIO CLOSED. *** Refuses with 409
        studio_open while Studio is running anywhere on this box (in-memory
        model stomps file-level edits — this corrupted a live demo once; no
        override exists) — use optix_deploy_updatesvc instead if Studio is
        open with the bridge armed. `editor_project_open` (409) means
        VS/VS Code has THIS project open.

        Mechanism: applies `edits` to the project tree, git-commits with
        `commit_message`, runs `FTOptixStudio.exe export`, atomically swaps
        the bundle into OPTIX_RUNTIME_DIR/<project>/, bounces the runtime,
        and probes the runtime port until it answers. UpdateSvc / OPC UA is
        NOT in this path.

        `project`: omit — defaults to the bridge's served project.

        `edits`: list of edit dicts, UTF-8 plaintext only. THREE MODES —
        prefer the anchored modes, fall back to full-content only for new
        files: (1) {"path", "find", "replace", "expect_count"?} — ANCHORED
        REPLACE, preferred for changing existing lines/values; `find` must
        occur exactly expect_count times (default 1) or the whole batch
        refuses (422 edit_anchor_mismatch) with nothing written. (2) {"path",
        "insert_after_anchor", "block"} — ANCHORED INSERT, preferred for
        adding nodes/widgets; `block` is inserted after the line containing
        the unique anchor, indent it yourself. (3) {"path", "content"} — FULL
        REPLACE, the only mode that can create a new file; for existing
        files prefer modes 1-2 to avoid drifting unrelated lines. The batch
        is atomic: ALL edits resolve against current disk state before ANY
        file is written; one mismatch refuses all. One edit per path per batch.

        `run_after_deploy`: True bounces + verifies the runtime (runtime_probe).
        False leaves it un-bounced (verification falls back to export_mtime)
        — for staged deploys where another agent flips the runtime separately.

        Use this when:
          - the user wants to apply specific edits and push to the local runtime
          - you've located the target via optix_find / optix_read_file and can
            anchor the change (modes 1-2)

        Do NOT use this when:
          - you have not read the target region first (blind full-content
            writes risk clobbering unrelated changes)

        Verify-half tip: the returned verification.method (runtime_probe)
        proves the runtime is up, NOT that your edit is visible — pair with
        optix_cdp_screenshot for visual confirmation.
        """
        dr = core.DeployRequest(
            edits=edits,
            commit_message=commit_message,
            run_after_deploy=run_after_deploy,
        )
        return core.deploy(cfg, project, dr)

    # ---- guided edit authoring ----------------------------------------
    # These RESOLVE edits and return them; they do not write. Forward the
    # returned `edits` to optix_deploy. Collect edits from several calls
    # into ONE optix_deploy (e.g. switch + label + model var = one deploy).

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_list_screens(project: str | None = None) -> dict:
        """List the Screen / Panel / Dialog nodes in a project's UI — the
        starting point for any UI edit.

        Returns {screens: [...], count, source}. `source` is "bridge" (live
        model, Studio + bridge running) or "file" (on-disk YAML). When Studio
        is open with this project and the bridge is up, reads live (no
        refusal); otherwise falls back to file mode, which still refuses with
        `studio_open` (409) if Studio is open with no bridge serving this
        project — no override either way.

        Use this when:
          - the user says "add X to the main screen" — find which screen that is
          - you need a screen's exact node name for optix_add_widget

        Do NOT use this when:
          - you already know the screen name AND file
        """
        return core.list_screens(cfg, project)

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_get_project_map(
        path: str | None = None, depth: int | None = None,
        max_nodes: int = 800, ids: bool = False, match: str | None = None,
        format: str = "outline", project: str | None = None,
    ) -> str | dict:
        """Component map of the project (names + nesting) in ONE call — the
        cheap way to learn structure instead of walking it with repeated
        optix_describe_node.

        Depth is AUTOMATIC: a FOLDER (or no path) gives an "overview" (one
        line per component, "(+N inside)"/"(N vars)"); a COMPONENT path (e.g.
        "UI/MainWindow") gives the full "detail" subtree at depth 6 (pass
        depth= to override). Pointers/bindings are DEREFERENCED inline
        ("Panel (NodePointer -> UI/Screens/ScreenA)") — a detail map doubles
        as a wiring audit.

        match="Pump*" turns the call into a LIVE-MODEL SEARCH (name or type).
        MATCHING IS EXACT unless you use * wildcards: match="Grid" only finds
        nodes named/typed exactly "Grid" — GridLayout1 needs "Grid*"/"*Grid*".
        Default to "*substring*" when hunting; a bare word silently returns 0
        hits for partial names. Use this instead of optix_find for live-model
        lookups (optix_find greps FILES, refused while Studio is open).

        IMPORTANT: shows MATERIALIZED nodes only — a property never set does
        NOT appear, and its absence never means you can't set it
        (optix_bridge_set_property materializes on write).

        format="outline" (default, human-readable) or format="json"
        (structured, for programmatic callers). ids=true adds NodeIds
        (rarely needed — tools address nodes by PATH).

        Use this when:
          - starting work on an unfamiliar project (no-path overview first)
          - you'd otherwise call optix_describe_node more than twice

        Do NOT use this when:
          - you need one node's property VALUES (optix_describe_node)
          - you need what a TYPE accepts (optix_describe_type)
          - Studio/the bridge is closed (bridge-only; arm the bridge first)
        """
        res = _bridge_guarded(project, lambda: core.get_project_map(
            cfg, project, path=path, depth=depth, max_nodes=max_nodes,
            ids=ids, match=match, fmt=format))
        if not isinstance(res, dict) or format == "json" or "map" not in res:
            return res
        # outline: plain text beats a JSON-escaped string (no \n noise, fewer
        # tokens); metadata folds into one header line
        hdr = f"# {res['project']} · {res['path']} · mode {res.get('mode')}"
        if res.get("mode") == "search":
            hdr += f" · match {res.get('match')} · {res.get('hit_count')} hits"
            if res.get("hits_capped"):
                hdr += " · CAPPED (raise max_nodes)"
        elif res.get("truncated"):
            hdr += " · TRUNCATED (raise max_nodes)"
        return hdr + "\n" + res["map"]

    @mcp.tool(annotations=_RO)
    def optix_bridge_status() -> dict:
        """Status of the design-time read-bridge(s) (NetLogic HTTP listener(s)
        in Studio).

        multi-instance (v1.0.7) — up to several Studio instances
        can each have an armed bridge SIMULTANEOUSLY, one per project, so this
        now returns `bridges`: a list, one entry per port currently answering
        {available, project, bridge_version, port, reason, source}, plus
        `count`.  `bridges[0]` (or {} when count==0) is echoed at the top
        level under the pre-1.0.7 single-bridge field names (`available`/
        `project`/`bridge_version`/`port`/`reason`/`source`) so an existing
        caller reading those still gets a sane single answer — but with more
        than one bridge armed, read `bridges` for the full picture; the
        top-level fields alone don't say WHICH project they describe among
        several.

        `source` reports how the bridge was discovered: `"registry"` when it
        was found via the per-port JSON registry files written by the C# bridge
        on bind (``%LOCALAPPDATA%\\ftx-mcp\\bridges\\<port>.json``), or
        `"scan"` when it was found by the legacy port-range scan.  Prefer
        `"registry"` entries — they skip the range scan entirely and clean up
        stale entries automatically.

        Use this when:
          - deciding whether live-model reads will work, or you're file-only
          - the user asks "is the bridge up / what project(s) are open in
            Studio?" or "which projects can I currently author against?"

        Do NOT use this when:
          - you just want to read a node — call optix_describe_node and handle
            the bridge_unavailable error instead of pre-checking
        """
        bridges = core.list_bridges(cfg)
        primary = bridges[0] if bridges else core.bridge_state(cfg)
        return {**primary, "bridges": bridges, "count": len(bridges)}

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_describe_node(path: str, project: str | None = None) -> dict:
        """Introspect one node in the LIVE model via the design-time bridge.

        `path` is an Optix model path (NO leading slash), e.g. "UI/MainWindow",
        "Model/Motor1". Returns the node's real type, children, and property
        values directly from Studio's in-memory model, with no YAML guessing.
        REQUIRES Studio open with this project AND the bridge running, else
        raises `bridge_unavailable` (503) — no file-path equivalent exists.

        Use this when:
          - you need a node's exact type or property schema before editing
          - you want to discover what a screen/panel contains (its children)

        Do NOT use this when:
          - Studio is closed (use optix_find / optix_read_file against files)
          - you need a full-text search (use optix_find)
        """
        return _bridge_guarded(project, lambda: core.describe_node(cfg, project, path))

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_list_ui_types(project: str | None = None) -> dict:
        """List the builtin UI type catalog from the LIVE model — "what controls
        exist?" (Label, Button, Rectangle, Panel, DataGrid, Trend, …), read from
        Studio's type system, not guessed. Bridge-only (else `bridge_unavailable`).
        Pair with optix_describe_type for a type's property schema.

        Use this when:
          - you need to know which control types are available before adding one

        Do NOT use this when:
          - you already know the type name (go straight to optix_describe_type)
          - Studio is closed (the bridge is down)
        """
        return _bridge_guarded(project, lambda: core.list_ui_types(cfg, project))

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_describe_type(
        type_name: str | None = None, type_names: list[str] | None = None,
        project: str | None = None,
    ) -> dict:
        """Property schema of a builtin UI type from the LIVE model — the "shape"
        of a control (which properties it has, their datatypes), so an edit can
        be composed against the real schema instead of guessed YAML. `type_name`
        is a catalog name from optix_list_ui_types (e.g. "Label", "Button").
        Bridge-only (else `bridge_unavailable`); `node_not_found` for an unknown type.

        Use this when:
          - before adding/setting a property, to confirm it exists and its datatype

        Do NOT use this when:
          - you want the properties of a specific existing NODE (optix_describe_node)
          - Studio is closed (the bridge is down)
        """
        if type_names:
            # batch form: one round trip for a type survey
            def _batch():
                out: dict = {"schemas": [], "errors": []}
                for tn in type_names:
                    try:
                        out["schemas"].append(core.describe_type(cfg, project, tn))
                    except core.BridgeUnavailable:
                        raise  # propagate to _bridge_guarded for structured response
                    except core.CoreError as e:
                        out["errors"].append({"type": tn, "error": str(e)})
                return out
            return _bridge_guarded(project, _batch)
        if not type_name:
            return {"error": "bad_request",
                    "message": "pass type_name or type_names"}
        return _bridge_guarded(project, lambda: core.describe_type(cfg, project, type_name))

    # ---- consolidated schema surface -----------------------------------
    # optix_schema collapses optix_schema_dump / _list / _diff into one
    # action-discriminated tool (same rationale as the U14 CDP consolidation:
    # fewer tools registered = fewer ToolSearch deferral round-trips). This
    # family is low-traffic internal plumbing (not the heavily-used CDP one),
    # so there is a CLEAN REPLACE — no deprecated per-action aliases. All
    # three actions were already _RO (read-only), so the merge is
    # annotation-uniform; no semantic split to reconcile.
    _SCHEMA_ACTIONS = ("dump", "list", "diff")

    @mcp.tool(annotations=_RO, name="optix_schema")
    def _optix_schema_tool(
        action: Literal["dump", "list", "diff"],
        project: str | None = None,
        version_a: str | None = None,
        version_b: str | None = None,
    ) -> dict:
        """Studio type-schema operations — ONE tool, pick an `action`.
        Consolidates the optix_schema_dump / _list / _diff family (each
        formerly its own tool); replaced cleanly, no deprecated aliases.

        action:
          - "dump" — fetch + cache the FULL type-schema dump for the running
            Studio version. Enumerates the whole builtin type catalog into an
            offline cache keyed by Studio version, then returns a compact
            SUMMARY (never the full dump — it's large). Powers offline reads
            and cross-version diffing (action="diff"). Requires Studio open +
            bridge + the bridge's /bridge/schema/dump endpoint; missing any
            returns `bridge_unavailable`. Uses `project` (omit for the
            bridge's served project).
          - "list" — list the Studio versions whose schema dumps are cached
            on disk. Pure offline read — no Studio required. Feed any two of
            these to action="diff". Ignores `project`/`version_a`/`version_b`.
          - "diff" — diff two cached schema dumps (upgrade intelligence,
            offline). Requires `version_a` (the older/from version) and
            `version_b` (the newer/to version); `changed_types` maps a type
            to its added/removed/changed properties (a changed prop = same
            name, different datatype or settable). Both versions must be
            cached (action="dump" on each box, or action="list" to see what's
            available); if either is missing, returns
            error='version_not_cached' with `missing`/`available`.

        An unknown `action`, or "diff" missing a required version, returns a
        structured error rather than raising.

        Use this when:
          - you want to snapshot the current Studio version's schema for
            offline use or later cross-version comparison ("dump")
          - you want to know which schema snapshots are available to diff
            ("list")
          - the user asks what changed in the type schema between two Studio
            versions — new types, dropped properties, datatype changes
            ("diff")

        Do NOT use this when:
          - you only need one type's shape (optix_describe_type)
          - Studio is closed and you need "dump" (the bridge is down) —
            "list" and "diff" work fully offline regardless
          - a version has not been dumped yet (run action="dump" there first,
            then "diff")
        """
        if action not in _SCHEMA_ACTIONS:
            return {
                "error": "bad_action",
                "message": (f"unknown action {action!r}; valid actions: "
                            f"{', '.join(_SCHEMA_ACTIONS)}"),
                "valid_actions": list(_SCHEMA_ACTIONS),
            }
        if action == "list":
            return {"versions": optix_schema.list_cached(cfg)}
        if action == "dump":
            proj = _resolve_project(project)
            if not proj:
                return _NO_PROJECT
            try:
                dump = optix_schema.ensure_dump(cfg, proj)
            except core.BridgeUnavailable:
                return {
                    "error": "bridge_unavailable",
                    "hint": ("schema dump needs Studio open + the bridge armed + "
                             "the /bridge/schema/dump endpoint (bridge build)"),
                }
            path = optix_schema.cache_path(cfg, dump.get("studio_version", ""))
            return optix_schema.dump_summary(dump, path)
        # action == "diff"
        if not version_a or not version_b:
            return {
                "error": "missing_param",
                "message": "action 'diff' requires version_a and version_b",
            }
        a = optix_schema.load_dump(cfg, version_a)
        b = optix_schema.load_dump(cfg, version_b)
        missing = [v for v, d in ((version_a, a), (version_b, b)) if d is None]
        if missing:
            return {
                "error": "version_not_cached",
                "missing": missing,
                "available": optix_schema.list_cached(cfg),
            }
        diff = optix_schema.schema_diff(a, b)
        diff["summary"] = {
            "version_a": version_a,
            "version_b": version_b,
            "added_types": len(diff["added_types"]),
            "removed_types": len(diff["removed_types"]),
            "changed_types": len(diff["changed_types"]),
        }
        return diff

    def _bridge_guarded(project: str, fn):
        """Run a live-model bridge write; on a bridge failure return a
        structured, nudging error the model can relay (never a raw exception).
        The bridge lives in the user's Studio, so we never auto-restart — we
        classify (down / wrong-project / loading / per-op) and tell the user
        exactly what to do. See core.classify_bridge_failure."""
        try:
            return fn()
        except (core.BridgeUnavailable, core.BridgeWriteFailed) as e:
            return core.classify_bridge_failure(cfg, project, e)

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_create_widget(
        screen: str, name: str, widget_type: str = "Label",
        project: str | None = None,
    ) -> dict:
        """Create a UI widget on a screen in the LIVE model via the bridge.

        Adds a builtin control (Label/Button/Rectangle/Panel/...) as a child
        of `screen` (an Optix path, NO leading slash, e.g. "UI/MainWindow").

        PLACEHOLDER-COLLECTION AUTO-ROUTING: some parents declare a named
        child collection (NavigationPanel.Panels, DataGrid.Columns,
        XYChart.Pens, gauges' WarningZones) — children belong INSIDE it, like
        Studio's drag-and-drop. Target the PARENT's path; the bridge
        auto-routes when widget_type fits (created_path/routed_into report
        the real placement). Errors are loud: ambiguous_container (pass the
        explicit sub-path) and read_only_collection (runtime-managed, never
        authorable). A NavigationPanelItem renders a ZERO-WIDTH tab until its
        Title is set — set Title right after creating.

        Live-model write, export-safe by construction. Requires Studio open
        with this project AND the bridge running (else bridge_unavailable).
        A new widget is STRUCTURAL — a running emulator won't show it until a
        restart cycle (optix_emulator(action="restart")).

        Use this when:
          - adding a control while Studio is open (the live, export-safe path)

        Do NOT use this when:
          - Studio is closed (live authoring needs Studio + the bridge)
          - the plan is create-then-bind: use optix_bridge_add_bound_widget
            instead — TRANSACTIONAL (a failed bind rolls back the create;
            hand-rolling create_widget + bind_property has no such guarantee)
          - you want a Folder / plain Object / custom-type instance (those are
            structural nodes — optix_bridge_create_folder/_object/_type)
        """
        return _bridge_guarded(project, lambda: core.bridge_create_widget(
            cfg, project, screen, name, widget_type))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_add_bound_widget(
        screen: str, name: str, widget_type: str,
        left: float | None = None, top: float | None = None,
        width: float | None = None, height: float | None = None,
        text: str | None = None,
        bind_property: str | None = None, source_path: str | None = None,
        mode: str = "Read",
        project: str | None = None,
    ) -> dict:
        """Create a widget, position it, and bind one property — in ONE call.

        The composite for the create -> set Left/Top/Width -> bind dance that
        every bound control (Switch, SpinBox, TextBox, ...) otherwise takes
        3-5 calls to build. Only the args you pass are applied. TRANSACTIONAL
        — a failure after creation rolls the created node back automatically
        ({ok: false, failed_step, rolled_back: true}), so a retry with the
        same name is always safe.

        Use this when:
          - adding any positioned and/or bound control (the common case)

        Do NOT use this when:
          - wiring EVENTS (optix_bridge_wire_event after creating)
          - attaching computed expressions (optix_bridge_attach_expression)
          - a plain static label (optix_bridge_add_label is one arg shorter)
        """
        return _bridge_guarded(project, lambda: core.bridge_add_bound_widget(
            cfg, project, screen, name, widget_type, left=left, top=top,
            width=width, height=height, text=text,
            bind_property=bind_property, source_path=source_path, mode=mode))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_add_navigation_panel_item(
        panel_path: str, title: str, screen_path: str | None = None,
        name: str | None = None, project: str | None = None,
    ) -> dict:
        """Add a tab to a NavigationPanel in ONE call: create the item (auto-
        routed into Panels), set its Title, and point it at a screen.

        Title is REQUIRED — an empty-Title item renders a zero-width invisible
        tab. screen_path (e.g. "UI/Screens/ScreenD") wires what the tab shows;
        omit it to wire later via set_property "Panel". Restart the emulator to
        see the new tab (structural edit).

        Use this when:
          - adding a navigation tab (the create/Title/Panel trio in one call)

        Do NOT use this when:
          - reordering tabs (optix_bridge_reorder)
          - retitling an existing tab (optix_bridge_set_property "Title")
        """
        return _bridge_guarded(project, lambda: core.bridge_add_navigation_panel_item(
            cfg, project, panel_path, title, screen_path=screen_path, name=name))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_add_label(
        screen: str, name: str, text: str,
        left: float | None = None, top: float | None = None, locale: str = "en-US",
        project: str | None = None,
    ) -> dict:
        """Add a Label with text (+ optional position) in ONE call via the live bridge.

        The common "put a label on the screen" case in a single round-trip:
        creates a Label named `name` on `screen` (Optix path, no leading slash),
        sets its Text, and — if given — LeftMargin/TopMargin. Equivalent to
        optix_bridge_create_widget + optix_bridge_set_property x1-3 in one call.
        Requires Studio open with this project AND the bridge running.

        Use this when:
          - the user wants a label on a screen with some text (the everyday case)
          - you'd otherwise chain create_widget + several set_property calls

        Do NOT use this when:
          - Studio is closed (live authoring needs Studio + the bridge)
          - the widget isn't a Label (use optix_bridge_create_widget)
        """
        return _bridge_guarded(project, lambda: core.bridge_add_label(
            cfg, project, screen, name, text, left=left, top=top, locale=locale))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_ensure_web_engine(
        port: int = 8081, ip: str = "0.0.0.0", project: str | None = None,
    ) -> dict:
        """Ensure a Web presentation engine exists so the runtime serves a canvas.

        Without a WebUIPresentationEngine under UI, a deployed runtime renders no
        web canvas (CDP verify has nothing to screenshot). Idempotent: returns
        {existed:true} if one is already present, else creates + configures one
        (Port, Protocol=HTTP, StartWindow → the first window). Requires Studio
        open with this project AND the bridge running. Run once during project
        setup, then author->save->deploy->verify has something to serve.

        Use this when:
          - setting up a new/scratch project for the deploy-verify loop
          - a deploy serves nothing / the CDP screenshot is blank (no web engine)

        Do NOT use this when:
          - Studio is closed (bridge-only; open Studio + StartBridge first)
        """
        return _bridge_guarded(project, lambda: core.bridge_ensure_web_engine(
            cfg, project, port=port, ip=ip))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_set_property(
        node_path: str, name: str, value: str, locale: str = "en-US",
        project: str | None = None,
    ) -> dict:
        """Set a property on a LIVE-model node via the design-time bridge.

        `node_path` is an Optix path (NO leading slash), `name` the property
        (Text, Width, FontSize, ...), `value` a string coerced to the
        property's type. Materializes a freshly-created instance's inherited
        property (e.g. a new Label's Text) so it persists AND renders — the
        fix for the GetVariable-null trap. Requires Studio open with this
        project AND the bridge running (else bridge_unavailable). To SEE it
        in a running emulator: needs a restart cycle (optix_emulator(action=
        "restart")) — the emulator renders its own loaded snapshot, not the
        live Studio model.

        Use this when:
          - setting a property on a node while Studio is open

        Do NOT use this when:
          - Studio is closed (live authoring needs Studio + the bridge)
          - the property is ARRAY-typed (String[]/NodeId[], "[]" suffix in
            describe): returns unsupported_array_write; author in Studio directly
          - you're DIAGNOSING why something doesn't render: read current
            values with optix_describe_node instead of writing presumed
            defaults "to rule things out" — each such write MATERIALIZES the
            property permanently while changing nothing (optix-verify-loop
            skill has the blank-render checklist)
        """
        return _bridge_guarded(project, lambda: core.bridge_set_property(
            cfg, project, node_path, name, value, locale))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_create_variable(
        name: str, parent: str = "Model", datatype: str = "Boolean",
        project: str | None = None,
    ) -> dict:
        """Create a model variable in the LIVE model via the design-time bridge.

        Adds a variable `name` of `datatype` (Boolean/Int32/Double/String) under
        `parent` (an Optix path, default "Model"). Live-model write, export-safe
        by construction. Requires Studio open with this project AND the bridge
        running. Persist with optix_save.

        Use this when:
          - adding a model variable while Studio is open
          - you'll bind a widget property to it next

        Do NOT use this when:
          - Studio is closed (live authoring needs Studio + the bridge)
          - you want a CONTAINER (optix_bridge_create_object) or a grouping
            folder (optix_bridge_create_folder)
        """
        return _bridge_guarded(project, lambda: core.bridge_create_variable(
            cfg, project, name, parent, datatype))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_create_folder(
        parent: str, name: str, project: str | None = None,
    ) -> dict:
        """Create a structural FOLDER in the LIVE model via the design-time bridge.

        Folders (OpcUa FolderType) are organizational nodes — Model subtrees,
        UI/Templates, grouping — NOT UI controls, which is why they aren't in
        optix_bridge_create_widget's catalog. `parent` is an Optix path
        ("Model", "UI"). Duplicate sibling names refuse loud (name_exists).
        Persist with optix_save.

        NOTE: the bridge does NOT auto-promote a widget dropped at the
        Templates root into an ObjectType (Studio does) — create types
        explicitly with optix_bridge_create_type.

        Use this when:
          - organizing Model/UI subtrees, creating a Templates folder
        Do NOT use this when:
          - you want a data-holding container (optix_bridge_create_object)
        """
        return _bridge_guarded(project, lambda: core.bridge_create_folder(
            cfg, project, parent, name))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_create_object(
        parent: str, name: str, object_type: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Create a plain OBJECT container — or an INSTANCE of a custom type —
        in the LIVE model via the design-time bridge.

        With no `object_type`: a BaseObjectType container (group variables
        under it with optix_bridge_create_variable). With `object_type` = a
        path to a project ObjectType (e.g. "UI/Templates/PumpCard"): creates an
        INSTANCE of that type — how you reuse a template made with
        optix_bridge_create_type / optix_bridge_convert_to_type. Passing an
        instance path errors not_a_type.

        Use this when:
          - structuring model data (Motor1 with Speed/Power/Running under it)
          - instantiating a custom template type onto a screen or into Model
        Do NOT use this when:
          - you want a builtin UI control (optix_bridge_create_widget)
          - you want a plain grouping node (optix_bridge_create_folder)
        """
        return _bridge_guarded(project, lambda: core.bridge_create_object(
            cfg, project, parent, name, object_type))

    @mcp.tool(annotations=_RW)
    def optix_bridge_create_netlogic(
        parent: str, name: str, project: str | None = None,
    ) -> dict:
        """Register a NetLogic (C#) node in the LIVE model via the bridge — the
        authoring step that used to require Studio's "New -> NetLogic" menu.

        A NetLogic node has NO generated proxy and NO code-reference property:
        the runtime binds it to the class whose name equals the node BrowseName,
        and the SDK-style NetSolution .csproj auto-globs every .cs. So the full
        recipe to add running C# is: (1) write the .cs (public class <name> :
        BaseNetLogic { ... }) into the project's NetSolution folder with the
        normal file tools, (2) call this with name == that class name, (3)
        restart the emulator so the rebuild instantiates it. `name` MUST match
        the class exactly.

        PLACEMENT matters: a runtime NetLogic that reads sibling nodes through
        Owner must be created under the object that owns those nodes (pass that
        object as `parent`, e.g. a screen). A self-contained one can live under
        any loaded container (a NetLogic category folder runs at app start).

        Returns {ok, created_path, bound_class, node_class}. After a C# rebuild
        the design-time bridge unloads (see the bridge note on emulator
        restart); restart it before further authoring.

        Use this when:
          - you have (or will write) a BaseNetLogic class and need its node
          - a screen/object needs code-behind and there is no node yet
        Do NOT use this when:
          - the class does not exist and you are not about to author it (the
            node will bind to nothing until a matching class compiles in)
          - you want a plain data object (optix_bridge_create_object) or a
            reusable template type (optix_bridge_create_type)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_create_netlogic(
            cfg, project, parent, name))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_create_type(
        name: str, parent: str, base_type: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Create an OBJECT TYPE (reusable template) in the LIVE model.

        `base_type` is a builtin UI type name (RowLayout, Button — see
        optix_list_ui_types) so the type renders like its base, OR a path to
        another project ObjectType (subtyping), OR omitted for a bare
        model-side structured type. Author the template's CONTENT by
        targeting the new type's path with the normal tools (create_widget/
        set_property/bind_property write into types exactly like into
        MainWindow, which IS a WindowType). Instantiate with
        optix_bridge_create_object (object_type=<this type's path>).

        PLAN-AHEAD workflow: create the type FIRST, author inside it, then
        instantiate everywhere. To promote an already-built instance instead,
        use optix_bridge_convert_to_type.

        Use this when:
          - building a reusable widget/template before any instance exists
          - defining structured model types (MotorType with Speed/Power)
        Do NOT use this when:
          - a one-off widget is enough (optix_bridge_create_widget)
        """
        return _bridge_guarded(project, lambda: core.bridge_create_type(
            cfg, project, name, parent, base_type))

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    @_with_project
    def optix_bridge_move_node(
        node_path: str, new_parent: str, new_name: str | None = None,
        project: str | None = None,
    ) -> dict:
        """MOVE (reparent) a live instance to a new parent — e.g. an existing
        column of widgets into a freshly-created ScrollView.

        Implemented as re-authoring (copy under the new parent, delete the
        original) — a raw node-model reparent corrupts the live model.
        Consequence: the node gets a NEW NodeId, so INBOUND references from
        elsewhere to the moved subtree are NOT rewritten (outbound bindings
        ARE re-created). `skipped` lists anything not copied. optix_save
        first; render-verify after (structural change — restart emulator).

        Use this when:
          - restructuring a screen (wrapping content in a new container)
          - a widget was created under the wrong parent
        Do NOT use this when:
          - other widgets bind INTO the subtree being moved (their links break
            — rebind after, or restructure around it)
          - you want a reusable template (optix_bridge_convert_to_type)
          - you only want z-order (optix_bridge_reorder)
        """
        return _bridge_guarded(project, lambda: core.bridge_move_node(
            cfg, project, node_path, new_parent, new_name))

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    @_with_project
    def optix_bridge_convert_to_type(
        node_path: str, type_name: str, types_folder: str = "UI/Templates",
        replace: bool = True, project: str | None = None,
    ) -> dict:
        """Convert a LIVE instance into a reusable ObjectType — Studio's
        right-click "Convert to Type" refactor, which has no public API.

        Creates `type_name` (subtyping the instance's own type) in
        `types_folder` (must exist — optix_bridge_create_folder first),
        RE-AUTHORS a copy of the subtree into it (fresh nodes, DynamicLinks
        re-created against resolved targets — live children are never
        re-parented, which corrupted the model before this fix), and with
        replace=true (default) swaps the original for an instance of the new
        type.

        READ `skipped` — constructs the copy can't reproduce (expression
        converters, exotic attachments, unresolvable link targets) are listed
        there, not silently half-copied; re-attach those by hand. Verify via
        links_verified/broken_links. optix_save first is cheap insurance;
        render-verify after (structural change — restart the emulator).

        Use this when:
          - an already-built widget assembly should become a template
        Do NOT use this when:
          - nothing is built yet (optix_bridge_create_type + author into it)
          - the node is already an ObjectType (already_a_type)
        """
        return _bridge_guarded(project, lambda: core.bridge_convert_to_type(
            cfg, project, node_path, type_name, types_folder, replace))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_bind_property(
        node_path: str, name: str, source_path: str | None = None,
        mode: str = "Read", raw_path: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Bind a node's property to a model variable (DynamicLink) via the bridge.

        Creates a live dynamic link so `node_path`.`name` tracks the model
        variable at `source_path`. `mode` in {Read, Write, ReadWrite}. The
        semantic "bind the label's Text to the status variable" op (vs a
        static set). Requires Studio open + the bridge running. Persist with
        optix_save.

        ALIAS/TEMPLATE binding uses `raw_path` INSTEAD of source_path: a
        literal NodePath like "{Alias1}/MyInt" or "../../Alias1/MyInt" that
        resolves PER INSTANCE at runtime — deliberately NOT resolvable at
        bind time; that late binding is what makes a template reusable. A
        source_path THROUGH an alias always fails source_not_variable — the
        signal to switch to raw_path. No validation on raw paths: render-verify.

        Use this when:
          - wiring a UI property to live data
          - binding template widgets through an alias slot (raw_path)

        Do NOT use this when:
          - you just want a static value (optix_bridge_set_property)
          - the widget doesn't exist yet: use optix_bridge_add_bound_widget
            for create+bind in one TRANSACTIONAL call — if this bind fails
            after a separate create_widget, the orphan widget stays on the
            screen; the composite rolls it back automatically
        """
        return _bridge_guarded(project, lambda: core.bridge_bind_property(
            cfg, project, node_path, name, source_path, mode, raw_path))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_create_alias(
        parent_path: str, name: str, target_path: str | None = None,
        kind: str | None = None, project: str | None = None,
    ) -> dict:
        """Create an alias under a node — the parameter slot of a reusable
        component/template.

        `kind` sets the TYPE CONSTRAINT ("BaseObject"/"Motor", or a project
        type path) — without it the alias is a bare NodeId pointer with no
        shape for validation. `target_path` is OPTIONAL and usually ABSENT on
        a template: each INSTANCE points the alias via
        optix_bridge_set_property(<instance>/<alias>, name="Value",
        value=<target path>). Bind the template's widgets THROUGH the alias
        with optix_bridge_bind_property(raw_path="{<name>}/<child>"). Requires
        Studio open + the bridge. Persist with optix_save.

        Use this when:
          - adding a parameter/data slot to a template type
          - making a widget reusable by aliasing its data target

        Do NOT use this when:
          - a plain dynamic link to a fixed variable suffices
            (optix_bridge_bind_property with source_path)
        """
        return _bridge_guarded(project, lambda: core.bridge_create_alias(
            cfg, project, parent_path, name, target_path, kind))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_add_translation(
        key: str, value: str, locale: str = "en-US",
        project: str | None = None,
    ) -> dict:
        """Add or update a translation for a LocalizedText key via the bridge.

        Registers `key` -> `value` for `locale` in the project's translation table
        (Add if new, Set if it exists). A UI Text holding that key then renders the
        translated string. Requires Studio open + the bridge. Persist with optix_save.

        Use this when:
          - adding i18n strings the UI references by key
          - localizing a label/message

        Do NOT use this when:
          - you want a literal one-off string (use optix_bridge_set_property)
          - Studio is closed
        """
        return _bridge_guarded(project, lambda: core.bridge_add_translation(
            cfg, project, key, value, locale))

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    @_with_project
    def optix_bridge_delete_node(node_path: str, project: str | None = None) -> dict:
        """Delete a node from the live model via the bridge.

        Removes the node at `node_path` (and its outbound references). Live-model
        op; requires Studio open + the bridge. Persist with optix_save. Check impact
        first if unsure (references endpoint, when available).

        Use this when:
          - removing a widget/variable you created
          - cleaning up scratch nodes

        Do NOT use this when:
          - you're unsure what references the node (you may break bindings)
          - Studio is closed
        """
        return _bridge_guarded(project, lambda: core.bridge_delete_node(
            cfg, project, node_path))

    # new tool, registered alongside the new
    # core.bridge_invoke_method wrapper -- exposes the bridge's generic
    # /bridge/node/invoke endpoint so any exported NetLogic method can be
    # triggered from here (originally requested to run Optix's built-in
    # broken-link finder/fixer without a manual Studio Execute click).
    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    @_with_project
    def optix_bridge_invoke_method(
        node_path: str, method_name: str, args: str | None = None,
        timeout_seconds: float = 60.0, unsafe: bool = False,
        project: str | None = None,
    ) -> dict:
        """Execute an exported UAMethod on a live-model object via the bridge.

        Runs the method on the bridge thread under a root Optix session, which
        is what makes it safe: without a session, ExecuteMethod killed
        FTOptixStudio.exe on any method (GitHub issue #4). Exceptions the
        method throws are caught and logged by Optix, and Studio stays up.
        Needs a bridge build that advertises `invoke_session` in
        /bridge/health; an older build is refused with
        `invoke_unsupported_bridge` before the call (rebuild + re-arm, or use
        optix_execute_method).

        `args`, if given, is a comma-separated string of positional input
        argument values. Whatever the method does is NOT undoable by this
        tool, so optix_save first when the method mutates the model.
        `unsafe` is a legacy flag and has no effect.

        `timeout_seconds` defaults to 60 (an arbitrary method's runtime is
        unknowable). A "timed out" error often means the method is still
        running, not that the bridge is down.

        Use this when:
          - running a design-time generator/helper [ExportMethod] without the
            right-click gesture (no Studio foreground needed, unlike
            optix_execute_method)

        Do NOT use this when:
          - a dedicated bridge_* wrapper already covers the operation
          - you don't know what the method does
          - Studio is closed (no live model to invoke against)
        """
        return _bridge_guarded(project, lambda: core.bridge_invoke_method(
            cfg, project, node_path, method_name, args=args,
            timeout=timeout_seconds, unsafe=unsafe))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_reorder(
        node_path: str,
        position: str | None = None, index: int | None = None,
        project: str | None = None,
    ) -> dict:
        """Change a node's z-order among its siblings via the bridge.

        Render order = child order: the LAST child renders in FRONT, the first
        BEHIND. Pass `position="front"`, `position="back"`, or an explicit
        `index`. Enables a Panel background: create a Rectangle, send it to
        back so it sits behind the panel's other children. Live-model op;
        requires Studio open + the bridge. Persist with optix_save. Reload the
        runtime page to see the visual change.

        Use this when:
          - a background rectangle needs to go behind existing widgets
          - bringing a control in front of / behind overlapping widgets

        Do NOT use this when:
          - the node is NOT inside a ScreenType/PanelType (MoveUp/Down no-ops
            outside a type — reorder silently has no effect)
          - Studio is closed (no live model to reorder)
        """
        return _bridge_guarded(project, lambda: core.bridge_reorder_node(
            cfg, project, node_path, position=position, index=index))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_attach_expression(
        node_path: str, prop_name: str,
        expression: str, sources: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Attach an ExpressionEvaluator converter to a property via the bridge.

        The ExpressionEvaluator is FT Optix's formula language — "a dumb Excel
        with fewer functions" — subsuming ConditionalConverter,
        LinearConverter, etc. `expression` uses `{0}`,`{1}`,... placeholders
        bound in order to `sources` (comma-separated paths). Functions:
        max/min/avg/abs/trunc/ceil/floor/round/sqrt/sign/like/isempty/`if`/
        left_of/right_of. Colors are `0xAARRGGBB`. Example: conditional color
        expression="if({0} > 40, 0xFFFF0000, 0xFF00FF00)", sources="Model/Speed"
        on FillColor. Requires Studio open + the bridge. Persist with
        optix_save. IMPORTANT: a converter no-ops SILENTLY if mis-wired —
        verify at runtime (deploy + screenshot), not just {ok:true}.

        Use this when:
          - a property must be COMPUTED from one or more sources — not a
            straight 1:1 bind

        Do NOT use this when:
          - the property just mirrors ONE source 1:1 (use optix_bridge_bind_property)
          - the logic exceeds the 17-function set (needs a custom C# converter)
          - Studio is closed
        """
        return _bridge_guarded(project, lambda: core.bridge_attach_expression(
            cfg, project, node_path, prop_name, expression, sources=sources))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_attach_formatter(
        node_path: str, prop_name: str,
        format: str, sources: str | None = None,
        mode: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Attach a FORMATTED DYNAMIC LINK to a property via the bridge.

        A DynamicLink whose path is BUILT at runtime by a StringFormatter from
        NAMED sources — Optix calls it a "formatted dynamic link", and in Studio
        it is one control: type `{#token}` into a dynamic link's path and it
        grows a child link per token.

            format  = "../NavPanel{#navIdx}@NodeId"
            sources = "navIdx=Model/GlobalVariables/AlarmVariables/AlarmTab"

        `{#name}` binds to a HasSource child called `Source<name>`, so the
        source NAME is load-bearing — unlike optix_bridge_attach_expression,
        whose sources are POSITIONAL (Source0..N) and which is hard-wired to
        ExpressionEvaluator. `sources` is name=path pairs separated by ';';
        `mode` is Read (default) / ReadWrite / Write.

        The `format` PATH is resolved by the runtime, not the bridge: write it
        node-relative ("../NavPanel{#i}@NodeId") or absolute
        ("/Objects/<Project>/Model/Folder/{#key}"). The project-relative form
        `sources` accepts ("Model/Folder/{#key}") resolves against the property
        itself and renders BLANK — ok:true, no runtime log line.

        THE indirect-address pattern: a PanelLoader's `Panel` chosen by an
        integer, with the candidate panels as readable named variables on the
        loader instead of opaque NodeId GUIDs buried in a ValueMapConverter.
        Equally the "{#value} {#eu}" unit-label shape on a Label's Text.
        Replaces whatever converter/link was on the property, so migrating off
        a ValueMapConverter needs no separate teardown.

        Requires Studio open + the bridge. Persist with optix_save. A converter
        no-ops SILENTLY if mis-wired, so {ok:true} means attached, never
        correct — restart the emulator and LOOK.

        Use this when:
          - a NodeId/text property is selected by an index or key at runtime and
            you want the candidates visible as named variables

        Do NOT use this when:
          - the property mirrors ONE source 1:1 (optix_bridge_bind_property)
          - it needs a COMPUTED value rather than a built path
            (optix_bridge_attach_expression)
          - Studio is closed
        """
        return _bridge_guarded(project, lambda: core.bridge_attach_formatter(
            cfg, project, node_path, prop_name, format,
            sources=sources, mode=mode))

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_bridge_validate_expression(
        expression: str, sources: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Syntax-check an ExpressionEvaluator formula WITHOUT attaching it.

        Optix validates a formula only at RUNTIME, where a bad one silently
        no-ops (the classic converter trap). This catches common author-time
        mistakes up front: unbalanced ()/{}, a placeholder {N} beyond the
        number of sources, an unknown function name, an unterminated string.
        The SAME check gates optix_bridge_attach_expression — this tool is for
        checking BEFORE you commit.

        Use this when:
          - drafting a non-trivial formula and you want it verified before wiring
          - debugging why a converter renders nothing (validate the expression first)

        Do NOT use this when:
          - the expression is a plain 1:1 bind (use optix_bridge_bind_property)
          - Studio/the bridge is closed
        """
        return _bridge_guarded(project, lambda: core.bridge_validate_expression(
            cfg, project, expression, sources=sources))

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_bridge_wire_event(
        node_path: str, event_type: str,
        method_path: str | None = None, command: str | None = None,
        variable: str | None = None, value: str | None = None,
        args: str | None = None, object_raw: str | None = None,
        replace: bool = False,
        project: str | None = None,
    ) -> dict:
        """Wire a UI event on a node — to a NATIVE command or a NetLogic method.

        Builds an EventHandler so `event_type` (e.g. MouseClickEvent) on
        `node_path` fires an action. Prefer a NATIVE command (no custom
        NetLogic): command="SetVariable" with variable=<path> + value=<v>, or
        command="ToggleVariable" with variable=<path> — wired to FT Optix's
        builtin VariableCommands; or a UI command — command="OpenDialog" /
        "CloseDialog" / "OpenKeyboard" — whose arguments ride in `args` as
        "Name=Value;Name=Value" (OpenDialog: Dialog=<Dialog type path>;
        AliasNode=<node or empty>;ParentItem= — an empty ParentItem is
        late-bound to the event node; OpenKeyboard takes KeyboardType,
        TargetVariable, ParentItem — three, not two). For custom logic, pass
        method_path ("ObjectPath/MethodName") to a NetLogic [ExportMethod],
        with `args` for its InputArguments ("NewPanel=UI/Screens/Home") and
        `object_raw` for a UI-hosted method: a NodePath RELATIVE TO THE EVENT
        NODE (e.g. "../Loader") that late-binds the call target per session —
        ChangePanel and friends do NOTHING without it, silently (B19).
        Requires Studio open + the bridge; persist with optix_save; verify it
        fires post-deploy.

        A pre-existing handler for the same event type is updated in place by
        default (replace=False); pass replace=True to delete the existing
        handler first and create a fresh one (delete-then-create).

        Use this when:
          - a button should set/toggle a variable (native command — no NetLogic)
          - a button should open/close a dialog or the keyboard (UI command + args)
          - a control should trigger a NetLogic [ExportMethod] (method_path
            [+ args] [+ object_raw when the method lives on a UI object])

        Do NOT use this when:
          - the event type isn't a builtin UI event (returns event_not_found)
          - Studio is closed
        """
        return _bridge_guarded(project, lambda: core.bridge_wire_event(
            cfg, project, node_path, event_type, method_path,
            command=command, variable=variable, value=value,
            args=args, object_raw=object_raw, replace=replace,
        ))

    @mcp.tool(annotations=_RW)
    def optix_save(project: str | None = None) -> dict:
        """Persist the open project to disk (sends Ctrl+S to Studio).

        Studio has no programmatic save API, so this focuses the project's
        window and sends ^s, then verifies by watching the node-YAML mtime
        advance. Call this AFTER bridge authoring when you need the edit ON
        DISK without running anything. Requires an interactive session and
        the project open in Studio.

        You usually DON'T need this: optix_emulator(action="run")'s F5 saves
        as part of staging. Saving does NOT push edits into an already-RUNNING
        emulator — structural changes need optix_emulator(action="restart").

        Use this when:
          - you need bridge edits on disk to read/verify the YAML (no run needed)

        Do NOT use this when:
          - you're about to optix_emulator(action="run") anyway (F5 handles saving)
          - you expect it to refresh a running emulator (it can't — restart it)
          - Studio is closed, or you authored via file-path tools (already on disk)
        """
        project = project or core.default_project(cfg)
        if not project:
            return {"saved": False, "error": "no project given and no bridge serving one — pass project or start the bridge"}
        return core.save(cfg, project)

    # ---- consolidated emulator-lifecycle surface -------------------------
    # optix_emulator collapses optix_run_emulator / optix_restart_emulator /
    # optix_stop_emulator / optix_emulator_status / optix_runtime_log_tail
    # into one action-discriminated tool (same rationale as the optix_schema/
    # optix_routes consolidation: fewer registered tools = fewer ToolSearch
    # deferral round-trips). Low-traffic lifecycle plumbing relative to the
    # heavily-used CDP surface -> CLEAN REPLACE, no deprecated aliases.
    # Annotation: "status"/"log" are read-only, "run"/"restart"/"stop" are
    # writes — the merged tool inherits the most-privileged shape (write, not
    # destructive), the same trade-off optix_routes/optix_bridge_edit make for
    # their most-privileged dispatched action/op. Scope: run/restart/stop were
    # "author" tier, status/log were "read" tier; the merged tool takes
    # "author" (most-privileged), tightening status/log the same way the
    # optix_routes merge tightened its get/list actions from "read" to
    # "author". SHELLS OUT (Studio F5/UIA + process probes) -> stays in
    # _OFFLOAD_TOOLS.
    _EMULATOR_ACTIONS = ("run", "restart", "stop", "status", "log")

    # optix_bridge_arm / optix_project: the cold-start pair. Deliberately NOT
    # wrapped in @_with_project — that resolves a missing project from the
    # SERVING BRIDGE, and at arm time there is by definition no bridge to ask.
    @mcp.tool(annotations=_RW, name="optix_bridge_arm")
    def _optix_bridge_arm_tool(
        action: Literal["arm", "stop"],
        project: str,
    ) -> dict:
        """Arm or stop the design-time bridge for a project WITHOUT a human at
        the keyboard — ONE tool, pick an `action`.

        action:
          - "arm"  — Execute StartBridge on the project's bridge NetLogic.
          - "stop" — Execute StopBridge (frees its port for another instance).

        `project` is REQUIRED and cannot be inferred: every other bridge tool
        defaults it from the serving bridge, and the whole point here is that
        no bridge is serving yet.

        Studio executes NOTHING from the NetSolution until an explicit
        right-click -> Execute, and its CLI has no execute verb, so this drives
        the real GUI gesture through UI Automation. It briefly takes the
        foreground and moves the cursor (same class of intrusion as optix_save's
        Ctrl+S and optix_emulator(action="run")'s F5), then restores both.

        Multi-instance safe by construction: the target window is resolved by
        the project name exposed in its tree (NOT the Win32 caption, which is
        'FactoryTalk Optix Studio' for every window), the fast-exit asks "is a
        bridge serving THIS project" rather than "is the base port open", and
        the verify asserts the newly-bound port's /bridge/health names this
        project. Returns {ok, state: already_armed|armed|stopped, port, ...}.

        If it answers studio_window_not_found, open the project first:
        optix_project(action="open").

        `project` does NOT have to live under the projects root. A project
        Studio opened from anywhere (a Desktop folder, another drive) also
        resolves, provided that running Studio names it on its command line —
        the result then carries resolved_from="studio_process", and it stops
        resolving when that Studio closes. optix_status(action="doctor",
        project=...) says which source answered.

        Use this when:
          - optix_bridge_status says a project has no bridge and you want to
            author into it — this is the whole cold-start path
          - you need a port back: action="stop" frees one of the four so
            another Studio instance can bind

        Do NOT use this when:
          - a bridge is already serving that project (it no-ops with
            state=already_armed, but optix_bridge_status is the cheaper check)
          - the project is not open in Studio — optix_project(action="open")
            first; arming cannot conjure a window
        """
        if action not in ("arm", "stop"):
            return {"ok": False, "error": "bad_action", "action": action}
        return core.bridge_arm(cfg, project, action=action)

    @mcp.tool(annotations=_RW, name="optix_execute_method")
    def _optix_execute_method_tool(
        project: str,
        node: str,
        method: str,
    ) -> dict:
        """Right-click -> Execute a design-time [ExportMethod] on a NetLogic
        node, WITHOUT a human at the keyboard.

        The generalisation of optix_bridge_arm (which is this, hard-wired to
        StudioMCPBridge/StartBridge). Use it to fire a project's own design-time
        generators and builders — a card builder's BuildCards, a converter
        script's Apply — from the agent instead of asking someone to right-click.

        `node` is the NetLogic's name (e.g. "FormattedLinkScript"), `method` the
        [ExportMethod] name (e.g. "Apply"). The node must be declared in the
        project's NetLogic YAML; the .cs file alone is not enough, because what
        gets clicked is the NODE's menu entry.

        THIS IS THE ONLY SAFE WAY to run a design-time method. Studio executes
        nothing from the NetSolution until an explicit Execute, and invoking one
        in-process on the bridge's HTTP thread runs it off the UI thread and
        CRASHES Studio (measured). This drives the real GUI gesture through UI
        Automation — it briefly takes the foreground and moves the cursor, then
        restores both.

        `ok: true` means THE CLICK LANDED, not that the method succeeded:
        arbitrary methods expose no signal to verify against (unlike the bridge
        port for arm/stop), and a mis-wired converter no-ops silently. Confirm
        the effect with optix_describe_node or a render, and read Studio's
        Output pane for the method's own logging.

        Use this when:
          - a project ships a design-time generator/builder you need to run
          - you are driving a one-off migration script from the agent

        Do NOT use this when:
          - you want the bridge up (optix_bridge_arm — it fast-exits on
            already_armed, which this cannot)
          - the project is not open in Studio (studio_window_not_found;
            optix_project(action="open") first)
        """
        if not node or not method:
            return {"ok": False, "error": "bad_args",
                    "nudge": "both `node` (NetLogic name) and `method` are required"}
        return core.execute_design_method(cfg, project, node=node, method=method)

    @mcp.tool(annotations=_RW, name="optix_project")
    def _optix_project_tool(
        action: Literal["open", "new"],
        project: str,
        template: str | None = None,
    ) -> dict:
        """Studio project lifecycle via its CLI — ONE tool, pick an `action`.

        action:
          - "open" — launch Studio on an existing project and WAIT until it is
            really loaded. The CLI returns immediately while Studio is still
            loading, so readiness is the window's project identity appearing,
            not process existence and not a sleep. Returns state=already_open
            when a window is already serving it.
          - "new"  — create a project under the projects root. REFUSES if the
            directory already exists; this tool never writes into one.
            `template` maps to --template.

        Pair with optix_bridge_arm to go from nothing to an authorable project
        with no human at the keyboard: optix_project(action="open") then
        optix_bridge_arm(action="arm").

        Use this when:
          - the project you need to author is not open in Studio yet
          - you want a fresh project scaffolded under the projects root

        Do NOT use this when:
          - you only need to KNOW what exists (optix_list_projects) — this
            launches Studio, which is slow and takes over the screen
          - the project is already open (it returns already_open, but
            optix_bridge_status usually answers the real question)
        """
        if action == "open":
            return core.project_open(cfg, project)
        if action == "new":
            return core.project_new(cfg, project, template=template)
        return {"ok": False, "error": "bad_action", "action": action}

    @mcp.tool(annotations=_RW, name="optix_emulator")
    def _optix_emulator_tool(
        action: Literal["run", "restart", "stop", "status", "log"],
        project: str | None = None,
        save_first: bool = False,
        lines: int = 100,
        contains: str | None = None,
    ) -> dict:
        """Emulator lifecycle — ONE tool, pick an `action`. Consolidates
        optix_run_emulator / optix_restart_emulator / optix_stop_emulator /
        optix_emulator_status / optix_runtime_log_tail (each formerly its own
        tool); replaced cleanly, no deprecated aliases.

        action:
          - "run" — F5: launch the project in Studio's built-in emulator. THE
            default verify step — much cheaper and faster than a deploy. F5
            stages the in-Studio model (saves as part of staging) and spins up
            a LOCAL FTOptixRuntime. F5 TOGGLES: check action="status" first so
            a blind "run" doesn't stop a running emulator. A RUNNING emulator
            does not pick up Studio edits (separate process, its own loaded
            snapshot) — structural changes (new widgets, bindings, layout)
            need action="restart". TARGET GUARD: F5 runs Studio's SELECTED
            deployment target; a non-emulator target refuses
            (active_target_not_emulator) — the service never changes the
            dropdown. `save_first` stages+saves before launching.
          - "restart" — stop-if-running -> start -> wait until serving, in ONE
            call. THE way to make a STRUCTURAL edit visible (new widget,
            binding, layout) — removes the F5-toggle footgun. No save needed
            (starting stages and saves the current Studio model).
          - "stop" — explicit, unambiguous stop (vs "run", which toggles and
            is easy to double-fire). Terminates ONLY emulator instances
            (--application-name=Emulator); an UpdateSvc-deployed runtime is
            the same exe and is left alone.
          - "status" — not_running / starting / running. Counts ONLY real
            emulator processes. state=starting means the port isn't serving
            yet — wait before screenshotting; running means safe to
            screenshot. Check this before "run" to avoid the F5-toggle trap.
          - "log" — tail the emulator's runtime log (NetLogic output,
            exceptions) — the debug signal when a preview misbehaves.
            Non-blocking: one brief shared read of the newest
            FTOptixRuntime.*.log (never poll this in a tight loop). `lines`
            caps the tail (default 100); `contains` filters case-
            insensitively. NOTE: the log is NOT rotated per restart — a
            `contains="error"` hit may be HOURS old; check timestamps before
            treating a match as current.

        `project`: used by "run"/"restart" (defaults to the bridge's served
        project; a structured no-project error if neither is given) and "log"
        (same resolution, required). Ignored by "stop"/"status" — those are
        process-level, not project-scoped. `save_first` only applies to
        "run". `lines`/`contains` only apply to "log".

        An unknown `action` returns a structured error rather than raising.

        Use this when:
          - previewing/verifying a bridge edit, or debugging why a preview
            looks wrong — pick the action for what you need (see above)

        Do NOT use this when:
          - you want the UpdateSvc-deployed runtime's lifecycle (that's
            optix_runtime_start/_stop/_status — a different process)
          - you want Studio's SELECTED deployment target (optix_active_target)
        """
        if action not in _EMULATOR_ACTIONS:
            return {
                "error": "bad_action",
                "message": (f"unknown action {action!r}; valid actions: "
                            f"{', '.join(_EMULATOR_ACTIONS)}"),
                "valid_actions": list(_EMULATOR_ACTIONS),
            }
        if action == "run":
            proj = project or core.default_project(cfg)
            if not proj:
                return {"launched": False, "error": "no project given and no bridge serving one — pass project or start the bridge"}
            return core.run_emulator(cfg, proj, save_first=save_first)
        if action == "restart":
            proj = project or core.default_project(cfg)
            if not proj:
                return {"launched": False, "error": "no project given and no bridge serving one — pass project or start the bridge"}
            return core.restart_emulator(cfg, proj)
        if action == "stop":
            return core.stop_emulator(cfg)
        if action == "status":
            return core.emulator_status(cfg)
        # action == "log"
        proj = _resolve_project(project)
        if not proj:
            return _NO_PROJECT
        return core.runtime_log_tail(cfg, proj, lines=lines, contains=contains)

    @mcp.tool(annotations=_RO)
    def optix_bridge_log_tail(lines: int = 100, contains: str | None = None) -> dict:
        """Tail the design-time bridge's transport diagnostics — the forensic
        trail for bridge drops/timeouts (why did :8768 go unreachable?).

        Each event is {ts, path, method, latency_ms, status, ok, error}. The log
        captures every transport FAILURE (timeout / connection aborted — e.g. a
        rebuild unloading the bridge NetLogic), every non-2xx, every real
        authoring op, and every health up<->down TRANSITION; the steady-state 2s
        health poll is deduped out so the log stays signal-dense. `contains`
        filters case-insensitively. Returns {file, mtime, events, returned,
        last_ok, ...} or {error:"no_bridge_log"} if no bridge call is logged yet.

        Use this when:
          - a bridge call just failed / the bridge 'dropped' and you want to see
            WHEN and WHY (pair with optix_bridge_status for the live state)
          - diagnosing intermittent bridge availability across a long session

        Do NOT use this when:
          - you want the live up/down state — that's optix_bridge_status
          - you want NetLogic/runtime output (exceptions, the reason a
            preview went blank) — that's optix_emulator(action="log")
        """
        return core.bridge_log_tail(cfg, lines=lines, contains=contains)

    @mcp.tool(annotations=_RO)
    def optix_active_target(project: str | None = None) -> dict:
        """Which deployment target Studio's dropdown has selected — the thing an
        F5 (optix_emulator action="run") would actually run.

        Reads the LIVE per-window selection off the bridge's Studio toolbar via
        UI Automation, accurate even when Studio's Configuration.xml is stale
        (it can say Emulator while the toolbar is really on a hardware panel).
        `source`="uia_live" is the definitive live read; the config-file path
        is the lazy fallback (off-Windows, no bridge, or non-interactive session).

        multi-instance (v1.0.7) — pass `project` to read a
        SPECIFIC armed bridge's Studio window when several are up at once.
        Omitted: resolves cleanly when 0 or 1 bridge is armed (unchanged
        behavior); with MORE than one armed and no `project`, this refuses to
        guess and returns {known:False, reason:"ambiguous_bridge",
        armed_projects:[...]} instead of silently reading whichever bridge
        happens to be on the lowest port.

        Use this when:
          - checking whether F5 is safe (is_emulator) BEFORE
            optix_emulator(action="run")
          - the user asks "what target is selected?" — this is the clean read
            (optix_emulator only reports the target as a refusal side effect)

        Do NOT use this when:
          - you want emulator PROCESS state (optix_emulator action="status")
        """
        return core.active_target(cfg, project=project)

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    def optix_deploy_updatesvc(
        project: str | None = None, run_after: bool = False,
        disable_source_transfer: bool | None = None,
        save_first: bool = True,
    ) -> dict:
        """Deploy a saved project via the FT Optix Application Update Service.

        THE SHIP STEP — deliberate, not the everyday verify. For iteration,
        use optix_emulator(action="run") + optix_cdp_screenshot instead (much
        faster, no transfer); deploy AFTER the emulator preview confirms the
        change.
        WORKS WITH STUDIO OPEN (contrast optix_deploy, which REFUSES while
        Studio is open): the `deploy` verb spawns its OWN short-lived Studio
        to build+transfer, so your interactive Studio + bridge stay up the
        whole time.

        Mechanism: runs the Studio `deploy` verb, which opens the SAVED
        project from disk, builds it, and transfers it to the UpdateSvc at
        the configured deploy IP (OPTIX_DEPLOY_IP / _USERNAME / _THUMBPRINT,
        OPTIX_STUDIO_DEPLOYMENT_PASSWORD). With run_after=True and a
        logged-in deploy user, the verb starts the runtime itself. Saves the
        project first by default (save_first) — the deploy reads disk, so
        unsaved edits would otherwise not ship.

        NOTE: when Studio is open the result carries a `build_race_warning` —
        ADVISORY ONLY (`deployed` is still true, the change is live); it just
        flags that the open Studio's build could race the verb's build (the
        verb retries and wins). Not a reason to close Studio.

        disable_source_transfer: skip sending the source .optix tree to the
        target (built runtime only — faster; the default). Pass False to
        force the source onto the target when you'll open/edit it there.

        Use this when:
          - the emulator preview looks right and you're shipping the change
          - shipping to a real device/UpdateSvc (multi-box, production)
          - you want the verb to deploy AND start the runtime in one call

        Do NOT use this when:
          - you're still iterating/verifying a change (optix_emulator(action=
            "run") is the fast default loop — deploy is the ship step)
          - the deploy account/cert aren't configured (run optix_status(
            action="doctor"))
        """
        project = project or core.default_project(cfg)
        if not project:
            return {"deployed": False, "error": "no project given and no bridge serving one — pass project or start the bridge"}
        return core.deploy_updatesvc(
            cfg, project, run_after=run_after,
            disable_source_transfer=disable_source_transfer, save_first=save_first)

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_add_widget(
        screen: str,
        widgets: list[dict],
        screen_file: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Author an edit that adds widget(s) to a screen — does NOT deploy.

        Generates correct Optix YAML (fresh GUIDs, right indentation, the
        proven binding shape). Forward `edits` to optix_deploy (combine with
        other authored edits into one deploy). Replaces hand-composing widget
        YAML — the thing that made "add a label" slow and error-prone.

        widgets: list of dicts, one per widget. Supported kinds:
          - label:  {kind:'label', name, text, left?, top?, width?, height?,
                     text_color?, font_size?, visible_bind?}
                    visible_bind="{Model}/PowerOn" binds Visible to a Boolean.
          - switch: {kind:'switch', name, checked_bind, left?, top?, width?, height?}
                    checked_bind="{Model}/PowerOn" is required (read+write).
        Add several widgets in ONE call to share a screen and a single edit.

        Refuses with `studio_open` / `editor_project_open` (409) while Studio
        or an attributed editor holds the project.

        Use this when:
          - the user wants a label / switch on a screen (the canonical case)

        Do NOT use this when:
          - the widget kind isn't label/switch yet (use an anchored
            optix_deploy edit; tier-2 adds more kinds)
          - the screen has no Children: block (rare; returns a structured
            error pointing you to an anchored edit)
        """
        return core.add_widget(cfg, project, screen, widgets, screen_file=screen_file)

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_add_model_variable(
        name: str,
        datatype: str = "Boolean",
        value: bool = False,
        model_file: str = "Nodes/Model/Model.yaml",
        project: str | None = None,
    ) -> dict:
        """Author an edit adding a read+write variable to Model — does NOT deploy.

        Returns {edits, file, variable, target_path, preview} where
        target_path is the "{Model}/<name>" you pass as a widget's
        visible_bind / checked_bind. Tier-1 supports Boolean (the demo's
        PowerOn). Forward `edits` to optix_deploy, usually alongside an
        optix_add_widget edit in the same deploy.

        Refuses with `studio_open` (409) while Studio is running.

        Use this when:
          - you are about to add a bound switch/label and need the backing
            Boolean it reads/writes

        Do NOT use this when:
          - the variable already exists (optix_find "Name: <name>" to check)
          - you need a non-Boolean type (tier-2; use an anchored edit)
        """
        return core.add_model_variable(
            cfg, project, name, datatype=datatype, value=value, model_file=model_file
        )

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_set_property(
        file: str,
        widget: str,
        property: str,
        value: str,
        project: str | None = None,
    ) -> dict:
        """Author a find/replace edit changing one inline property — no deploy.

        Changes an inline shorthand property (Text, Left, Top, Width, Height,
        TextColor, ...) on a named widget. Returns {edits, widget, property,
        old_value, new_value}. Forward `edits` to optix_deploy. `value` is the
        raw YAML scalar — quote text yourself, e.g. value='"Hello Optix"'.

        Refuses with `studio_open` (409) while Studio is running.

        Use this when:
          - the user wants to retitle/move/resize an existing widget
            (e.g. set Text on a label, bump Left/Top)

        Do NOT use this when:
          - the property is a child-node (a binding, an expanded variable) —
            returns structural_edit_unsupported; use an anchored optix_deploy
            edit (optix_find -> ranged read -> anchored find/replace)
          - you are adding a NEW property the widget doesn't have yet
        """
        return core.set_property(cfg, project, file, widget, property, value)

    @mcp.tool(annotations=_RO)
    @_with_project
    def optix_deploy_preflight(project: str | None = None) -> dict:
        """Run every deploy precondition without launching Studio.

        Returns {ready, blockers, warnings, checks}; ready=True iff blockers is
        empty. Checks: project resolves + has .optix manifest, studio_exe
        present, runtime_dir configured, interactive_session=True (Windows
        DPAPI constraint), deploy lock free, git status, runtime port probe
        (informational), and the corruption guard (blocker `studio_open` when
        Studio is running; `editor_project_open` when VS/VS Code has this
        project open — remediation is closing the app, no override exists).

        Use this when:
          - about to run optix_deploy on a fresh box and want to catch
            missing config before consuming a Studio launch
          - a prior optix_deploy returned `failed` with no clear cause and
            you want a structured precondition report

        Do NOT use this when:
          - you already ran a successful deploy in this session (stale
            preflight signal value)
        """
        return core.deploy_preflight(cfg, project)

    @mcp.tool(annotations=_RW)
    @_with_project
    def optix_runtime_start(
        port: int | None = None,
        timeout: float | None = None,
        project: str | None = None,
    ) -> dict:
        """Launch FTOptixRuntime against the swapped runtime tree for `project`.

        Spawns the FTOptixRuntime.exe that Studio's export bundled into
        OPTIX_RUNTIME_DIR/<project>/FTOptixApplication/, detached from the
        service process. Polls the runtime port for tcp_reachable until
        `timeout` seconds elapse (default 30). Same Windows interactive-
        session DPAPI constraint as Studio. `port` defaults to
        cfg.runtime_test_port (typically 8081). Runtime tree must already
        exist (deploy first).

        Use this when:
          - bringing up a freshly-deployed project's runtime for CDP/user
          - after optix_deploy with run_after_deploy=False
          - restarting a runtime to pick up a change (pair with
            optix_runtime_stop first)

        Verify handoff: Optix Web renders into a single <canvas> (no DOM
        targets) — use optix_cdp_screenshot / optix_cdp_click for state
        verification, not synthetic DOM events (they no-op).

        Do NOT use this when:
          - you only want to check if a runtime is already up
            (use optix_runtime_status)
          - the project has not been deployed yet (raises runtime_binary_not_found)
        """
        return core.runtime_start(cfg, project, port=port, timeout=timeout)

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    @_with_project
    def optix_runtime_stop(project: str | None = None) -> dict:
        """Stop FTOptixRuntime processes attached to `project`'s runtime tree.

        WMI-matches FTOptixRuntime.exe processes whose CommandLine references
        the project's runtime tree, then Stop-Process -Force. Idempotent —
        stopping when nothing is running is a successful no-op. Other
        projects' runtimes are not touched.

        Use this when:
          - bouncing a runtime to pick up a code/asset change (call before
            re-deploying or before optix_runtime_start)
          - cleaning up a stale runtime that's holding the port

        Do NOT use this when:
          - you want to stop all FTOptixRuntime processes regardless of project
            (this only kills the ones bound to this project's tree)
        """
        return core.runtime_stop(cfg, project)

    @mcp.tool(annotations=_RO)
    def optix_runtime_status(slot: str) -> dict:
        """Probe a runtime instance's reachability.

        slot: 'test' (default port 8081) | 'mgmt' (default port 8086, a
        second operator-dashboard runtime, if you run one).

        Use this when:
          - confirming a deploy actually landed (after optix_deploy with
            run_after_deploy=False, the caller is responsible for cycling
            the runtime; this probe confirms)
          - cold-start drift check after a Windows reboot

        Do NOT use this when:
          - you want to know whether Studio is installed (optix_status(
            action="health"))
          - you want deploy outcome details (optix_status(action="services"))
        """
        return core.runtime_status(cfg, slot)

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    def optix_cdp_click(
        x: float, y: float, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        """Click (x, y) on the Optix runtime canvas via CDP — the RELIABLE path.

        Optix Web renders to a single <canvas> (no DOM targets) and synthetic
        DOM clicks no-op on buttons/switches. This injects a trusted CDP
        Input.dispatchMouseEvent that actually reaches Optix's hit-tester.
        Pass navigate_url to point Chrome at the runtime first; omit it to
        click whatever Chrome shows. Pair with optix_cdp_screenshot to read
        coordinates first.

        Use this when:
          - you need to actually trigger a button/switch on the running HMI
            (state changes), or navigate a NavigationPanel tab
          - a deploy landed but you want to confirm an interaction works

        Do NOT use this when:
          - the chrome-cdp task isn't running (returns cdp_unavailable; run
            optix_status(action="doctor") / services.ps1 status)
          - you haven't established coordinates from a screenshot first
        """
        return core.cdp_click_runtime(
            cfg, x=x, y=y, navigate_url=navigate_url, settle_seconds=settle_seconds)

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    def optix_cdp_fill(
        x: float, y: float, text: str,
        submit: str | None = "Enter", select_all: bool = True,
        navigate_url: str | None = None, settle_seconds: float | None = None,
        project: str | None = None,
    ) -> dict:
        """Update a field on the running HMI in ONE call: click (x, y), type
        `text`, commit with `submit` (default Enter — values don't stick
        without it).

        THE default way to set a TextBox/SpinBox value — replaces the
        click -> type -> key trio. select_all (default true) makes the typed
        text REPLACE the current value (a bare click only places a caret, so
        typing would append). submit=None types without committing;
        submit="Tab" for tab-commit fields. Fails loud with no_focused_input
        when the click didn't land on an editable field.

        Use this when:
          - setting a TextBox / SpinBox / editable field to a value (the
            common case — one call, not three)

        Do NOT use this when:
          - stepping a SpinBox with arrows (optix_cdp_key "ArrowUp"/"ArrowDown")
          - cancelling an edit (optix_cdp_key "Escape")
          - you want to screenshot mid-entry before committing (use
            optix_cdp_click + optix_cdp_type, then commit separately)
          - clicking a button/switch/tab (optix_cdp_click)
        """
        return core.cdp_fill_runtime(
            cfg, x=x, y=y, text=text, submit=submit, select_all=select_all,
            navigate_url=navigate_url, settle_seconds=settle_seconds,
            project=_resolve_project(project))

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    def optix_cdp_type(
        text: str, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        """Type a string into the focused field on the running Optix HMI (CDP
        Input.insertText).

        The keyboard half of the click-type-Enter pattern: optix_cdp_click the
        field FIRST, then this inserts the string at the caret/selection. Does
        NOT click and does NOT commit: **values don't stick until Enter** —
        follow with optix_cdp_key("Enter"). For the common set-a-field-value
        case, prefer optix_cdp_fill (click+type+commit in one call); this
        primitive is for mid-entry screenshots and non-standard flows. Fails
        loud with no_focused_input when nothing editable has focus.

        Use this when:
          - filling a TextBox / SpinBox / editable field on the live canvas
            (after a focusing click)
          - overwriting a SpinBox value (click auto-selects it; typing replaces)

        Do NOT use this when:
          - you haven't clicked the field yet (click first, then type)
          - you want to commit — that's optix_cdp_key("Enter"), a separate call
          - you're setting a model property (optix_bridge_set_property is the
            authoring path; this drives the RUNTIME UI like a user)
        """
        return core.cdp_type_runtime(
            cfg, text=text, navigate_url=navigate_url, settle_seconds=settle_seconds)

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    def optix_cdp_key(
        key: str, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        """Press one named key on the running Optix HMI (CDP dispatchKeyEvent,
        keyDown+keyUp).

        THE commit step for field edits: after optix_cdp_click + optix_cdp_type,
        optix_cdp_key("Enter") is what makes the value stick — without it the
        edit is discarded on blur. Keys: Enter, Escape, Tab, Backspace, Delete,
        ArrowUp/Down/Left/Right. KNOWN LIMIT: arrow keys do NOT step an Optix
        SpinBox — click its < / > stepper buttons instead. Unknown keys return
        invalid_key + the valid list. Pressing with no pending edit is a safe
        no-op.

        Use this when:
          - committing a typed TextBox/SpinBox value (Enter) — then screenshot
            to verify the bound model/label updated
          - cancelling an in-progress edit (Escape)
          - stepping a SpinBox without typing (ArrowUp/ArrowDown)

        Do NOT use this when:
          - you want to type text (optix_cdp_type)
          - nothing was clicked/focused and you expect an effect (no-op)
        """
        return core.cdp_key_runtime(
            cfg, key=key, navigate_url=navigate_url, settle_seconds=settle_seconds)

    @mcp.tool(annotations=_RO)
    def optix_cdp_screenshot(
        save_path: str | None = None, quality: int = 65,
        navigate_url: str | None = None, settle_seconds: float | None = None,
        fresh: bool = False, return_image: bool = False,
        region: list[float] | None = None, project: str | None = None,
    ):
        """Screenshot the running Optix HMI (emulator or deployed runtime) via
        CDP — THE way to visually verify a change.

        IMPORTANT — if your edit is NOT in the screenshot, do NOT conclude it
        failed: a running emulator renders its own loaded snapshot and does
        not pick up Studio edits. optix_emulator(action="restart"), then screenshot
        again before diagnosing. fresh=true forces a page reload before
        capture (stale-frame suspected).

        *** Runtime-verify tool: use THIS to confirm a deploy rendered — do
        NOT open the runtime in a general web browser (Cowork's visualize, a
        Mac/host browser). *** Drives the local chrome-cdp on the SAME box as
        the runtime (loopback) — no external browser, no network exposure.

        Omit navigate_url to auto-navigate to the local runtime (skips reload
        if already there, so click->re-screenshot keeps state); pass
        navigate_url="" to screenshot the current tab as-is.

        This tool ALWAYS writes the JPEG to a file and returns its `path` —
        read it back with your file tool. It does NOT put base64 in the JSON:
        a large b64 string makes some hosts try to *render* it inline
        (Cowork's "visualize"), which can hang on a sandboxed/headless host.
        **Prefer passing your own `save_path`**; omit it for a temp path.
        `return_image=true` ALSO returns the capture as TYPED MCP image
        content (not b64-in-JSON) so the model sees it inline — prefer this
        when your file tool cannot reach the service's filesystem; combine
        with region= to keep the payload small.

        region: optional [x, y, w, h] to capture a sub-rectangle instead of
        the full frame. Convention: all four values <= 1.0 are normalized
        viewport fractions; any value > 1 means absolute pixels. A malformed
        region returns state='failed', error='bad_region' rather than
        raising; the result's `region` echoes the resolved absolute-pixel box.

        Use this when:
          - VALIDATING a deploy: capture the runtime HMI to confirm the change is live
          - capturing the HMI to locate a widget before optix_cdp_click
          - zooming into one widget/region instead of the whole canvas (region)

        Do NOT use this when:
          - the chrome-cdp task isn't running (returns cdp_unavailable; run
            optix_status(action="doctor") / services.ps1 status)
        """
        if not save_path:
            import os
            import tempfile
            import time
            d = os.path.join(tempfile.gettempdir(), "ftx-cdp-screenshots")
            os.makedirs(d, exist_ok=True)
            save_path = os.path.join(d, f"runtime-{int(time.time() * 1000)}.jpg")
        result = core.cdp_screenshot_runtime(
            cfg, save_path=save_path, quality=quality,
            navigate_url=navigate_url, settle_seconds=settle_seconds,
            fresh=fresh, region=region, project=_resolve_project(project))
        if result.get("state") == "succeeded":
            result["hint"] = (
                "JPEG written to `path` - read it with your file tool. If your "
                "file tool cannot reach that path, re-call with save_path inside "
                "your workspace, or return_image=true to receive the image inline."
            )
        if return_image and result.get("state") == "succeeded" and result.get("path"):
            # Typed MCP image content (ImageContent block), NOT b64 stuffed in the
            # JSON text - the b64-in-JSON shape is what stalled Cowork's visualize
            # (see docstring). Metadata rides along as a JSON text block.
            from mcp.server.fastmcp import Image as _McpImage
            return [json.dumps(result), _McpImage(path=result["path"])]
        return result

    @mcp.tool(annotations=_RO)
    def optix_cdp_ocr(
        navigate_url: str | None = None, settle_seconds: float | None = None,
        psm: int = 6, project: str | None = None,
    ) -> dict:
        """OCR the runtime canvas via tesseract — an OPT-IN, text-only read-back.

        Prefer optix_cdp_screenshot + a vision model for verify (reads color,
        layout, AND text). Use THIS only when vision isn't available (headless/
        cron run) or as a fallback when a capture renders blank. If tesseract
        isn't installed, returns state='failed', error='tesseract_not_installed'
        (optional infrastructure — never crashes the loop).

        Use this when:
          - a headless caller has no vision model but needs to read back rendered text
          - a screenshot came back blank and you want any text signal at all

        Do NOT use this when:
          - a vision model is available (use optix_cdp_screenshot — it sees more)
          - you need to verify color/position (OCR is text-only)
        """
        return core.cdp_ocr_runtime(
            cfg, navigate_url=navigate_url, settle_seconds=settle_seconds,
            psm=psm, project=_resolve_project(project))

    @mcp.tool(annotations=_RO)
    def optix_cdp_read_text(
        region: list[float] | None = None, navigate_url: str | None = None,
        settle_seconds: float | None = None, psm: int = 6,
        project: str | None = None,
    ) -> dict:
        """OCR a region of the runtime canvas via tesseract — THE cheap check for
        "does the screen/widget say X" — zero vision tokens.

        Same region coordinate convention as optix_cdp_screenshot (values all
        <= 1.0 are normalized viewport fractions; > 1 means absolute pixels).
        Omit `region` to OCR the full frame. Same degradation contract as
        optix_cdp_ocr: missing tesseract returns error='tesseract_not_installed'
        rather than raising; a malformed region returns error='bad_region'.

        Use this when:
          - checking that a specific label/widget shows expected text, cheaply
            (no vision model call) — e.g. confirming a SpinBox value after a fill
          - a headless/cron caller has no vision model but needs a targeted text read

        Do NOT use this when:
          - you need color/layout verification (use optix_cdp_screenshot + vision)
          - you don't know where the text is yet (use optix_cdp_find_text to locate
            it first, or omit region to read the whole frame)
        """
        return core.cdp_read_text_runtime(
            cfg, region=region, navigate_url=navigate_url,
            settle_seconds=settle_seconds, psm=psm,
            project=_resolve_project(project))

    @mcp.tool(annotations=_RO)
    def optix_cdp_find_text(
        text: str, navigate_url: str | None = None,
        settle_seconds: float | None = None, project: str | None = None,
    ) -> dict:
        """Locate `text` on the runtime canvas via tesseract word boxes — to find
        a labeled control to click, or to build a navigation route.

        Full-frame capture (you don't know coordinates yet). Case-insensitive;
        a multi-word `text` query only matches ADJACENT words on the same
        tesseract line. Words scoring below 40/100 are dropped before
        matching. `confidence` is a fraction in [0, 1] — same scale as
        optix_cdp_ocr/read_text. No match is NOT an error (found=false,
        matches=[]). Same degradation contract as optix_cdp_ocr for missing
        tesseract.

        `matches[].center_px` feeds optix_cdp_click directly — e.g. find "Start",
        then click at its center_px — without eyeballing coordinates from a
        screenshot.

        Use this when:
          - you need to click a labeled control but don't know its coordinates
          - building a navigation route by locating menu/button labels in sequence

        Do NOT use this when:
          - you already know the target coordinates (use optix_cdp_click directly)
          - you need to read a specific known region's text (use
            optix_cdp_read_text with a region — cheaper, no full-frame OCR)
        """
        return core.cdp_find_text_runtime(
            cfg, text, navigate_url=navigate_url, settle_seconds=settle_seconds,
            project=_resolve_project(project))

    # ---- routes file management (S7) -----------------------------------
    # MOTIVATION: a Cowork field test needed to CREATE a routes file for
    # optix_cdp_navigate/optix_cdp_sweep and, having no MCP tool to do it,
    # the model reached for host folder-access permission instead — its own
    # sandboxed file tools cannot see this service's filesystem. These three
    # tools make the service own routes files end-to-end so no client ever
    # needs local file access: optix_cdp_find_text (discover) ->
    # optix_routes_save (bank) -> optix_cdp_navigate/optix_cdp_sweep (replay).

    # ---- consolidated routes surface -----------------------------------
    # optix_routes collapses optix_routes_save / _get / _list into one
    # action-discriminated tool (same rationale as optix_schema above: fewer
    # registered tools = fewer ToolSearch deferral round-trips). Low-traffic
    # internal plumbing, so a CLEAN REPLACE — no deprecated per-action
    # aliases. Annotation note: "save" writes, "get"/"list" only read: the
    # merged tool is annotated _RW (can write) rather than _RO, the same
    # trade-off optix_bridge_edit makes for its most-privileged dispatched
    # op. Scope note: TOOL_SCOPES["optix_routes"] = "author" (was "read" for
    # get/list, "author" for save) — a deliberate tightening so a bare `read`
    # token can no longer call action="get"/"list"; this family sees no
    # meaningful read-only traffic separate from save, so the simpler single
    # scope was preferred over threading action-aware scope resolution
    # through _required_tool_scope.
    _ROUTES_ACTIONS = ("save", "get", "list")

    @mcp.tool(annotations=_RW, name="optix_routes")
    def _optix_routes_tool(
        action: Literal["save", "get", "list"],
        project: str,
        routes: dict | None = None,
        name: str = "ftx_ui_map",
    ) -> dict:
        """Routes-file CRUD — ONE tool, pick an `action`. Consolidates
        optix_routes_save / _get / _list (each formerly its own tool);
        replaced cleanly, no deprecated aliases.

        Routes files live in the SERVICE (a project's `dev/<name>.json`) —
        do NOT ask the user for host folder access; these three actions are
        the entire CRUD surface for them. `project` is required for every
        action (no bridge-default fallback — routes files are project-scoped
        by path, not by what Studio has open).

        action:
          - "save" — CREATE/REPLACE a routes file, the bank half of the
            routes-banking loop for optix_interact(action="navigate") /
            optix_cdp_sweep. Requires `routes`: either the full versioned
            shape (`{"version": 1, "routes": {"<name>": {"steps": [...]}}}`)
            or a bare `{"<name>": {"steps": [...]}}` mapping — both
            normalize on disk. Each route's `steps` is validated BEFORE
            anything is written: a malformed step anywhere fails
            error='routes_invalid' naming the offending route/step, rather
            than writing a partial file. Saved at `<project>/dev/<name>.json`
            (default "ftx_ui_map"). `name` is sanitized to `[a-zA-Z0-9._-]` —
            anything else fails error='bad_name' before touching disk.
            Saving over an existing `name` REPLACES its content wholesale
            (not a merge). `path` in the result is directly usable as
            `routes_path`.
          - "get" — read back a routes file saved via "save". A bad `name`
            fails error='bad_name' before touching disk (same sanitization
            rule as "save"); a missing file fails 'routes_file_not_found'
            (with `path`); malformed JSON fails 'routes_file_invalid'. Never
            raises for a missing/bad file. Ignores `routes`.
          - "list" — list every routes file saved under the project's
            `dev/` — what's already banked, before you navigate/sweep or
            save more. Only files parsing as a valid routes file (JSON
            object with a dict "routes" key) are listed; anything else under
            dev/*.json is skipped silently and counted in `skipped` — one
            bad file never hides the rest. No dev/ directory yet is not an
            error (files=[], count=0). Ignores `routes`/`name`.

        An unknown `action` returns a structured error rather than raising.

        Use this when:
          - you've discovered a click sequence (via optix_observe
            find_text/screenshot) and want to bank it for cheap replay
            ("save")
          - inspecting what routes/steps a banked file actually contains,
            e.g. before editing it via "save" ("get")
          - you don't remember what routes files (or route names) already
            exist before saving a new one or navigating ("list")

        Do NOT use this when:
          - the routes file already has the route you need — just
            optix_interact(action="navigate") by name, no need to re-save
          - you only want to replay a route (optix_interact reads the file
            directly)
          - you don't know the file's `name` yet ("list" first, then "get")
        """
        if action not in _ROUTES_ACTIONS:
            return {
                "error": "bad_action",
                "message": (f"unknown action {action!r}; valid actions: "
                            f"{', '.join(_ROUTES_ACTIONS)}"),
                "valid_actions": list(_ROUTES_ACTIONS),
            }
        if action == "save":
            if routes is None:
                return {
                    "error": "missing_param",
                    "message": "action 'save' requires routes",
                }
            return core.routes_save(cfg, project, routes, name=name)
        if action == "get":
            return core.routes_get(cfg, project, name=name)
        # action == "list"
        return core.routes_list(cfg, project)

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    def optix_cdp_navigate(
        route: str, routes_path: str, expect: bool = True,
        navigate_url: str | None = None, project: str | None = None,
    ) -> dict:
        """Zero-screenshot navigation to a banked screen: replays a sequence of
        clicks from a routes JSON file instead of screenshot -> locate -> click,
        click again.

        Routes file format (version 1): `{"version": 1, "routes": {"<name>":
        {"steps": [{"click": [x, y], "settle_seconds": 0.5, "expect_text":
        "Setup Values"}]}}}`. `click` uses the SAME coordinate convention as
        optix_cdp_screenshot's `region` — portable across window sizes.
        Convention: bank routes at `dev/ftx_ui_map.json` (see the
        optix-blind-authoring skill). `routes_path` accepts the `path`
        returned by optix_routes(action="save") directly.

        Routes files live in the service (not visible to client-side file
        tools) — do NOT ask the user for host folder access.

        expect_text verification needs tesseract: with expect=true (default)
        and a step carrying expect_text, this OCRs the frame after the click
        and checks expect_text is a case-insensitive substring. The FIRST
        failed expectation stops the route immediately (error=
        'expectation_failed' with the step index) — fail loud rather than
        drift onto the wrong screen. If tesseract isn't installed, checks are
        skipped (not a failure; ocr_unavailable=true) — clicks still ran.

        Never raises for a bad routes file: missing path ->
        'routes_file_not_found'; bad JSON -> 'routes_file_invalid'; unknown
        route -> 'route_not_found'; malformed step -> 'route_invalid'.

        Use this when:
          - jumping straight to a screen you've already banked a route for,
            without spending a screenshot to find your way there
          - a multi-step navigation (menu -> submenu -> tab) you'll repeat
            often — record it once, replay it cheaply from then on

        Do NOT use this when:
          - the route isn't banked yet (use optix_cdp_find_text /
            optix_cdp_screenshot to discover it first, then save the route)
          - you only need one click (use optix_cdp_click directly)
          - you need to verify color/layout, not just text (pair with an
            optix_cdp_screenshot after navigating)
        """
        return core.cdp_navigate_runtime(
            cfg, route=route, routes_path=routes_path, expect=expect,
            navigate_url=navigate_url, project=_resolve_project(project))

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    def optix_cdp_sweep(
        routes_path: str, out_dir: str, routes: list[str] | None = None,
        warmup: bool = True, project: str | None = None,
    ) -> dict:
        """Capture a full-frame screenshot (+ OCR text, if tesseract is
        installed) of every route in a banked routes file, in ONE CDP
        session — builds the visual baseline optix_cdp_diff compares against.

        Loads routes_path exactly like optix_cdp_navigate (same format;
        `routes_path` accepts the `path` from optix_routes(action="save")
        directly).
        Routes files live in the service — do NOT ask the user for host
        folder access.

        Sweeps all routes in file order, or just the names in `routes` — an
        unknown name fails error='route_not_found'. Steps replay like
        optix_cdp_navigate's clicks, but expect_text checks are ALWAYS off
        (capture pass, not verification). Between routes the tab returns to
        the runtime's home screen.

        warmup=True (default) takes and discards one capture first, giving
        the canvas a settle period. Writes <out_dir>/<route>.jpg and
        <out_dir>/manifest.json. A capture failure on one route does not
        abort the sweep — recorded as {"error": ...}; response carries
        "errors": N.

        Use this when:
          - building or refreshing a visual baseline of the whole HMI for
            optix_cdp_diff, after routes are banked
          - capturing every known screen in one pass instead of per-screen

        Do NOT use this when:
          - you only need one screen right now (optix_cdp_screenshot is
            cheaper and doesn't need banked routes)
          - the routes aren't banked yet (discover with optix_cdp_find_text
            first, then bank a routes file)
        """
        return core.cdp_sweep_runtime(
            cfg, routes_path=routes_path, out_dir=out_dir, routes=routes,
            warmup=warmup, project=_resolve_project(project))

    @mcp.tool(annotations=_RO)
    def optix_cdp_diff(dir_a: str, dir_b: str, threshold: float = 2.0) -> dict:
        """Compare two optix_cdp_sweep capture directories screen-by-screen
        — a visual regression check, no CDP session needed (pure file
        comparison).

        Matches screens by route key from each dir's manifest.json; a screen
        in only one dir is reported under `added`/`removed` (not an error).
        With Pillow installed (`pip install ftx-mcp[visual]`), grayscale
        mean-pixel-difference vs `threshold` (percent, default 2.0) decides
        changed/same, with a size mismatch short-circuiting to
        'size_mismatch'. Without Pillow, degrades to a text-only compare
        using each manifest's OCR text (degraded='no_pillow'); with neither
        Pillow nor OCR text this fails (error='no_pillow_no_ocr').

        TEXT is its own channel, independent of the pixel threshold: every
        screen gets text_added/text_removed and a text_changed flag. READ
        text_changed FIRST for label/value edits — a single-label change
        moves well under 1% of pixels on a busy screen, so pixel status
        stays 'same' at the 2.0 default. Live-updating values naturally churn
        text deltas between sweeps — treat process-value-shaped deltas as
        benign.

        Use this when:
          - checking whether a deploy/edit changed the rendered HMI,
            screen by screen, against a prior optix_cdp_sweep baseline
          - you want a cheap pass/fail signal before spending a vision
            model on a screenshot-by-screenshot comparison

        Do NOT use this when:
          - you haven't run optix_cdp_sweep on both sides yet (there's no
            manifest.json to compare)
          - you need a live look at the CURRENT canvas (use
            optix_cdp_screenshot — this tool never touches CDP)
        """
        return core.cdp_diff_runtime(dir_a, dir_b, threshold=threshold)

    @mcp.tool(annotations=_RW)
    def optix_cdp_restart(allow_restart: bool = True) -> dict:
        """Recover the chrome-cdp instance that screenshot/click drive.

        Usually you do NOT need this — screenshot/click self-heal on their own
        (open a page if Chrome is up but tab-less, or restart the
        ftx-mcp-chrome-cdp task if Chrome is down). Call this to force that
        recovery explicitly, or to check/repair after a reboot. Set
        allow_restart=False to only open a page (never relaunch the process).

        Use this when:
          - a verify tool reported cdp_unavailable and you want to repair it
          - after a reboot, to bring canvas-verify back without a full restart

        Do NOT use this when:
          - things are working — the tools already self-heal; this is a manual
            override, not a routine step
        """
        return core.ensure_chrome_cdp(cfg, allow_restart=allow_restart)

    # ---- consolidated CDP surface (U14) --------------------------------
    # optix_observe / optix_interact collapse the 12 optix_cdp_* tools into a
    # mode/action-discriminated pair so an agent sees ~4 CDP tools instead of
    # 12 (the 10 read/interact primitives folded in here, plus the two batch/
    # lifecycle tools optix_cdp_sweep / optix_cdp_restart kept as-is). The 10
    # folded originals stay registered as DEPRECATED thin aliases (same core.*
    # delegation) behind FTXMCP_LEGACY_TOOLS so no existing config breaks; set
    # FTXMCP_LEGACY_TOOLS=0 to expose the consolidated surface only. Schema uses
    # a plain Literal discriminator + per-branch optional params rather than a
    # nested pydantic discriminated union: FastMCP builds the input schema from
    # the raw signature, and a flat param set with a runtime valid-vocab nudge
    # is the robust-and-shipping shape (see the invalid-mode branch).
    _OBSERVE_MODES = ("screenshot", "ocr", "read_text", "find_text", "diff")
    _INTERACT_ACTIONS = ("click", "fill", "type", "key", "navigate")

    @mcp.tool(annotations=_RO)
    def optix_observe(
        mode: Literal["screenshot", "ocr", "read_text", "find_text", "diff"],
        # screenshot / read_text
        region: list[float] | None = None,
        save_path: str | None = None, quality: int = 65,
        fresh: bool = False, return_image: bool = False,
        # ocr / read_text
        psm: int = 6,
        # find_text
        text: str | None = None,
        # diff
        dir_a: str | None = None, dir_b: str | None = None,
        threshold: float = 2.0,
        # shared CDP-capture params
        navigate_url: str | None = None, settle_seconds: float | None = None,
        project: str | None = None,
    ):
        """Read-side capture of the running Optix HMI via CDP — ONE tool, pick a
        `mode`. Consolidates the optix_cdp_screenshot / _ocr / _read_text /
        _find_text / _diff family; the per-mode tools remain as deprecated
        aliases (FTXMCP_LEGACY_TOOLS).

        mode:
          - "screenshot" — full-canvas (or `region`) JPEG; THE visual-verify
            path. Writes to `save_path` (temp if omitted); `return_image=true`
            ALSO returns typed MCP image content inline (no file round-trip).
            `fresh=true` forces a reload before capture.
          - "ocr" — tesseract text read-back of the whole canvas (headless
            fallback when no vision model). Honors `psm`.
          - "read_text" — tesseract OCR of a `region` (or full frame): the
            cheap zero-vision "does it say X" check.
          - "find_text" — locate `text` on the canvas (word boxes + clickable
            centers) to drive a click or build a route. Requires `text`.
          - "diff" — compare two optix_cdp_sweep capture dirs screen-by-screen
            (pure file compare, no CDP). Requires `dir_a`, `dir_b`.

        `region`: all four values <= 1.0 are normalized viewport fractions,
        any value > 1 is absolute pixels. An unknown `mode` returns a
        structured error rather than raising.

        The emulated CSS viewport this tool captures is the authoritative
        coordinate frame for optix_interact: coordinates (normalized or
        absolute) passed to click/fill are resolved against the same viewport
        dimensions reported here.

        `settle_seconds` applies to every mode — the sleep fires before the
        capture regardless of whether `navigate_url` is supplied. Pass
        `settle_seconds=0` to disable.

        OCR can't resolve SMALL controls: full-frame tesseract renders small
        button labels as garbage (find_text returns found:false — not an
        error). Drive small controls by coordinates + a `screenshot` (vision)
        read-back instead.

        Use this when:
          - reading anything BACK from the live canvas — verify a deploy,
            check a label's text, locate a control, or a visual-regression diff

        Do NOT use this when:
          - you need to CHANGE the runtime (click/fill/type/key/navigate) —
            that's optix_interact
          - the chrome-cdp task isn't running (returns cdp_unavailable; run
            optix_status(action="doctor"))
        """
        if mode not in _OBSERVE_MODES:
            return {
                "state": "failed", "error": "bad_mode",
                "message": (f"unknown mode {mode!r}; valid modes: "
                            f"{', '.join(_OBSERVE_MODES)}"),
                "valid_modes": list(_OBSERVE_MODES),
            }
        if mode == "screenshot":
            sp = save_path
            if not sp:
                import tempfile
                import time as _t
                d = os.path.join(tempfile.gettempdir(), "ftx-cdp-screenshots")
                os.makedirs(d, exist_ok=True)
                sp = os.path.join(d, f"runtime-{int(_t.time() * 1000)}.jpg")
            result = core.cdp_screenshot_runtime(
                cfg, save_path=sp, quality=quality,
                navigate_url=navigate_url, settle_seconds=settle_seconds,
                fresh=fresh, region=region, project=_resolve_project(project))
            if result.get("state") == "succeeded":
                result["hint"] = (
                    "JPEG written to `path` - read it with your file tool. If "
                    "your file tool cannot reach that path, re-call with "
                    "save_path inside your workspace, or return_image=true to "
                    "receive the image inline."
                )
            if (return_image and result.get("state") == "succeeded"
                    and result.get("path")):
                # Typed MCP image content (see optix_cdp_screenshot) — the List
                # return shape is why optix_observe carries NO -> dict.
                from mcp.server.fastmcp import Image as _McpImage
                return [json.dumps(result), _McpImage(path=result["path"])]
            return result
        if mode == "ocr":
            return core.cdp_ocr_runtime(
                cfg, navigate_url=navigate_url, settle_seconds=settle_seconds,
                psm=psm, project=_resolve_project(project))
        if mode == "read_text":
            return core.cdp_read_text_runtime(
                cfg, region=region, navigate_url=navigate_url,
                settle_seconds=settle_seconds, psm=psm,
                project=_resolve_project(project))
        if mode == "find_text":
            return core.cdp_find_text_runtime(
                cfg, text, navigate_url=navigate_url,
                settle_seconds=settle_seconds,
                project=_resolve_project(project))
        # mode == "diff"
        return core.cdp_diff_runtime(dir_a, dir_b, threshold=threshold)

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    def optix_interact(
        action: Literal["click", "fill", "type", "key", "navigate"],
        # click / fill
        x: float | None = None, y: float | None = None,
        # fill / type
        text: str | None = None,
        submit: str | None = "Enter", select_all: bool = True,
        # key
        key: str | None = None,
        # navigate
        route: str | None = None, routes_path: str | None = None,
        expect: bool = True,
        # shared CDP params
        navigate_url: str | None = None, settle_seconds: float | None = None,
        project: str | None = None,
    ) -> dict:
        """Act on the running Optix HMI via CDP — ONE tool, pick an `action`.
        Consolidates optix_cdp_click / _fill / _type / _key / _navigate; the
        per-action tools remain as deprecated aliases (FTXMCP_LEGACY_TOOLS).

        action:
          - "click" — trusted CDP mouse click at (`x`, `y`); the reliable path
            Optix's canvas needs. Requires `x`, `y`.
          - "fill" — ONE call: click (`x`, `y`) -> select-all -> type `text` ->
            commit with `submit` (default Enter; None types without
            committing). THE way to set a TextBox/SpinBox. Requires `x`, `y`,
            `text`.
          - "type" — insert `text` into the already-focused field (no click,
            no commit). Requires `text`.
          - "key" — press one named `key` (Enter/Escape/Tab/Backspace/Delete/
            Arrow*), the commit step for edits. Requires `key`.
          - "navigate" — replay a banked click `route` from `routes_path`
            (zero-screenshot navigation). Requires `route`, `routes_path`.

        Coordinates use the shared convention (<= 1.0 = normalized viewport
        fractions, > 1 = absolute pixels). Either convention works
        interchangeably: normalized values are resolved against the emulated
        CSS viewport that optix_observe captures, so a fraction derived from a
        screenshot coordinate and its equivalent absolute pixel value dispatch
        to the same point. An unknown `action`, or a missing required param,
        returns a structured error rather than raising.

        Result fields for click/fill: `css_x` and `css_y` are the coordinates
        actually dispatched (authoritative over the caller-supplied `x`/`y`);
        `viewport` reports `{w, h}` used for normalization; `coords` is
        `"normalized"` or `"absolute"` to indicate which convention was used.

        `settle_seconds` is honoured for every action — the sleep fires after
        the CDP event is dispatched regardless of whether `navigate_url` was
        supplied. Pass `settle_seconds=0` to disable.

        VERIFY, don't trust the return: state="succeeded" means the CDP event
        was DISPATCHED, not that anything changed — a click at the wrong
        coordinate still returns succeeded. Always follow with a read-back
        (optix_observe, or optix_describe_node).

        Use this when:
          - you need to CHANGE the runtime — press a button, set a field
            value, commit an edit, or jump to a banked screen

        Do NOT use this when:
          - you only need to READ the canvas (screenshot/ocr/find) — that's
            optix_observe
          - the chrome-cdp task isn't running (returns cdp_unavailable; run
            optix_status(action="doctor"))
        """
        if action not in _INTERACT_ACTIONS:
            return {
                "state": "failed", "error": "bad_action",
                "message": (f"unknown action {action!r}; valid actions: "
                            f"{', '.join(_INTERACT_ACTIONS)}"),
                "valid_actions": list(_INTERACT_ACTIONS),
            }

        def _missing(*names):
            miss = [n for n in names if locals_map.get(n) is None]
            if miss:
                return {
                    "state": "failed", "error": "missing_param",
                    "message": (f"action {action!r} requires: "
                                f"{', '.join(miss)}"),
                    "required": list(names),
                }
            return None

        locals_map = {"x": x, "y": y, "text": text, "key": key,
                      "route": route, "routes_path": routes_path}
        if action == "click":
            err = _missing("x", "y")
            if err:
                return err
            return core.cdp_click_runtime(
                cfg, x=x, y=y, navigate_url=navigate_url,
                settle_seconds=settle_seconds)
        if action == "fill":
            err = _missing("x", "y", "text")
            if err:
                return err
            return core.cdp_fill_runtime(
                cfg, x=x, y=y, text=text, submit=submit, select_all=select_all,
                navigate_url=navigate_url, settle_seconds=settle_seconds,
                project=_resolve_project(project))
        if action == "type":
            err = _missing("text")
            if err:
                return err
            return core.cdp_type_runtime(
                cfg, text=text, navigate_url=navigate_url,
                settle_seconds=settle_seconds)
        if action == "key":
            err = _missing("key")
            if err:
                return err
            return core.cdp_key_runtime(
                cfg, key=key, navigate_url=navigate_url,
                settle_seconds=settle_seconds)
        # action == "navigate"
        err = _missing("route", "routes_path")
        if err:
            return err
        return core.cdp_navigate_runtime(
            cfg, route=route, routes_path=routes_path, expect=expect,
            navigate_url=navigate_url, project=_resolve_project(project))

    # ---- U16: batched authoring -----------------------------------------
    # One tool that validates a whole op batch through the bridge BEFORE
    # applying any of it. Ops are plain dicts discriminated by an `op` field
    # rather than a pydantic union: the same reason optix_observe/_interact use
    # a flat param set with a runtime valid-vocabulary nudge — a nested union
    # generates a schema some MCP clients mangle, and the flat shape is the
    # robust-and-shipping one. The per-noun optix_bridge_* tools stay as-is;
    # this batches them, it does not replace them.
    _EDIT_OPS = tuple(sorted(core.BRIDGE_EDIT_VERBS))

    @mcp.tool(annotations=_RW_DESTRUCTIVE)
    @_with_project
    def optix_bridge_edit(
        ctx: Context,
        ops: list[dict] = [],
        action: str = "apply",
        batch_id: str | None = None,
        dry_run: bool = False,
        strict: bool = False,
        chunk_seconds: float | None = None,
        chunk_ops: int | None = None,
        project: str | None = None,
    ) -> dict:
        """Apply a BATCH of live-model authoring ops, validated as a whole first.

        Each op is a dict with an `op` field naming the verb plus that verb's
        fields, e.g. [{"op": "create_widget", "screen": "UI/MainWindow",
        "name": "Gauge1", "widget_type": "Rectangle"}, {"op": "set_property",
        "path": "UI/MainWindow/Gauge1", "name": "Width", "value": "120"}].
        Valid ops: set_property, bind, create_widget, create_variable,
        create_folder, create_object, create_type, create_alias, delete, move,
        rename, reorder, wire_event, attach_expression, attach_formatter,
        attach_string_formatter, retype, add_translation.

        DISPLAY PRECISION: {"op": "attach_string_formatter", "path": "UI/.../Label1",
        "prop_name": "Text", "format": "{0:F1}", "sources": "Model/Temp"} puts a
        StringFormatter on the property so a Float renders "67.7". attach_formatter
        builds a formatted LINK instead and renders empty as a value formatter.

        RETYPE: {"op": "retype", "path": "Model/Recipe/Setpoints",
        "datatype": "String"} changes a VARIABLE's DataType in place (Float ->
        String here), keeping the node, its children (DynamicLink, Mode,
        converters) and every inbound link; the value is converted
        element-wise (22.3f -> "22.3"). `dims` omitted keeps the array shape,
        "scalar" collapses it, "N" sets it. Works on StoreColumns too. THE way
        to move recipe values to String storage.

        RENAME: {"op": "rename", "path": "UI/Screens/Foo", "new_name": "Bar"}
        renames a node (the name shown in Studio's tree). Rename re-authors
        the node in place, so it gets a NEW NodeId; inbound references from
        elsewhere are not rewritten. set_property with name=DisplayName sets
        the node's DisplayName ATTRIBUTE (LocalizedText; dedicated safe
        route) — Studio's tree then labels the node "BrowseName (DisplayName)"
        (verified live 2026-08-16); use rename to change the BrowseName part.
        BrowseName itself is never settable directly (it is the node's
        identity) — the attempt is refused.

        WHY BATCH: the bridge validates the ENTIRE list before anything is
        written, against a hypothetical model that accumulates this batch's
        own creates/deletes — so "create a node then set a property on it"
        validates clean, while the reverse order is caught up front with the
        offending `op_index` instead of half-applying. Validation runs FIRST
        and applies nothing on failure (state="validated" with errors), so a
        separate dry_run pass is NOT needed to be safe — reserve
        `dry_run=True` for a genuinely risky batch you want to inspect first.
        `strict=True` promotes lint warnings into errors.

        SIZE: batch ONE component's related ops — a widget plus its
        properties plus its binding — NOT a whole screen; a 100-op
        mega-batch gambles the whole screen on one partial-failure point.

        CHUNKING: large batches stop early and return state="chunked" when the
        time limit (OPTIX_BATCH_CHUNK_SECONDS, default 90 s) or op-count limit
        (OPTIX_BATCH_CHUNK_OPS, default 40) is reached; use `chunk_seconds` /
        `chunk_ops` to override per call. Pass the returned `batch_id` to
        action="continue" to apply the next chunk. `applied` counts ops landed
        so far — read it the same way as state="partial". MCP progress
        notifications are best-effort and silently absent without a
        progressToken — the journal (action="status") is the authoritative
        record of what landed.

        NOT ATOMIC, and the result says so: if op N fails at apply time, ops
        0..N-1 stay applied (state="partial", `applied` counting what landed).
        Read `applied` — never assume a failure means nothing happened.

        PER-OP OUTCOMES: each `op_timings` entry carries `detail` when the
        bridge reported how the op landed — `via` (wire_event:
        route token ending +existing / +replaced / no suffix = created), `relative_sources` (links made
        type-relative inside an ObjectType), `achieved` (reorder's final
        index), `prior binding cleared` (bind).

        action:
          - "apply"    — validate and begin applying `ops` (default).
          - "continue" — apply the next chunk of a chunked batch; requires
            `batch_id` from a prior state="chunked" response.
          - "status"   — return the journal for `batch_id`, or the most recent
            N batch journals for the project when `batch_id` is omitted.

        Use this when:
          - you have several related edits for ONE component — batch them so
            they validate and land together

        Do NOT use this when:
          - you have ONE edit — the per-noun tool is simpler
          - Studio is closed or the bridge is down
        """
        # C.5: action dispatch — bad_ops / bad_op_verb pre-checks only for apply.
        if action == "continue":
            if not batch_id:
                return {
                    "state": "failed", "error": "missing_batch_id",
                    "message": "action='continue' requires a batch_id",
                }
            return _bridge_guarded(
                project, lambda: core.bridge_edit_continue(cfg, project, batch_id))

        if action == "status":
            return _bridge_guarded(
                project, lambda: core.bridge_edit_status(cfg, project, batch_id))

        if action != "apply":
            return {
                "state": "failed", "error": "bad_action",
                "message": (f"unknown action {action!r}; valid: apply, continue, status"),
                "valid_actions": ["apply", "continue", "status"],
            }

        # action == "apply": validate ops before dispatching.
        if not isinstance(ops, list) or not ops:
            return {
                "state": "failed", "error": "bad_ops",
                "message": "ops must be a non-empty list of op objects",
                "valid_ops": list(_EDIT_OPS),
            }
        bad = [
            {"index": i, "op": o.get("op") if isinstance(o, dict) else None}
            for i, o in enumerate(ops)
            if not isinstance(o, dict) or o.get("op") not in core.BRIDGE_EDIT_VERBS
        ]
        if bad:
            return {
                "state": "failed", "error": "bad_op_verb",
                "message": (
                    "each op needs an `op` field naming a valid verb; "
                    f"offending: {bad}"
                ),
                "valid_ops": list(_EDIT_OPS),
            }

        # C.3: progress callback — anyio.from_thread.run bridges the sync tool
        # thread to the async event loop; core's defensive try/except suppresses
        # any failure (e.g. no event loop on the main thread in tests).
        on_progress = (
            lambda ev: anyio.from_thread.run(  # noqa: E731
                ctx.report_progress, ev["index"], ev["total"], ev.get("message"))
        )

        # C.5: apply chunk-threshold overrides via env vars for the duration of
        # this call. Not thread-safe for concurrent callers with different
        # overrides, but acceptable: defaults are per-process constants and
        # overrides are rare per-call adjustments.
        _env_backup: dict[str, str | None] = {}
        if chunk_seconds is not None:
            _env_backup["OPTIX_BATCH_CHUNK_SECONDS"] = os.environ.get(
                "OPTIX_BATCH_CHUNK_SECONDS")
            os.environ["OPTIX_BATCH_CHUNK_SECONDS"] = str(chunk_seconds)
        if chunk_ops is not None:
            _env_backup["OPTIX_BATCH_CHUNK_OPS"] = os.environ.get(
                "OPTIX_BATCH_CHUNK_OPS")
            os.environ["OPTIX_BATCH_CHUNK_OPS"] = str(chunk_ops)
        try:
            return _bridge_guarded(project, lambda: core.bridge_edit(
                cfg, project, ops, dry_run=dry_run, strict=strict,
                on_progress=on_progress, batch_id=batch_id))
        finally:
            for _k, _v in _env_backup.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v

    if not cfg.enable_deploy:
        # MCP deploy integration is statically disabled in this distribution. The
        # standard loop is author -> emulator preview -> verify; shipping
        # happens from Studio's own Deploy dialog. Hiding the deploy/runtime
        # family (and the file-edit authoring that feeds optix_deploy) keeps
        # the default catalog lean and free of deploy credentials vocabulary.
        for _t in ("optix_deploy", "optix_deploy_updatesvc",
                   "optix_deploy_preflight", "optix_runtime_start",
                   "optix_runtime_stop", "optix_runtime_status",
                   "optix_add_widget", "optix_add_model_variable",
                   "optix_set_property"):
            mcp._tool_manager._tools.pop(_t, None)

    # U14 consolidation: the 10 optix_cdp_* primitives were folded into
    # optix_observe / optix_interact. The DEFAULT surface is now
    # consolidated-only — the aliases are NOT registered. Setting
    # FTXMCP_LEGACY_TOOLS=1 is the opt-in escape hatch for existing configs:
    # it restores the 10 deprecated aliases (each delegating to the same
    # core.* functions) with a deprecation prefix on its client-visible
    # description. Any other value (unset / "0" / anything != "1") keeps the
    # consolidated-only default and pops the aliases. optix_cdp_sweep /
    # optix_cdp_restart are NOT aliases — they are the batch/lifecycle tools
    # kept as-is and always registered.
    _CDP_ALIASES = (
        "optix_cdp_screenshot", "optix_cdp_ocr", "optix_cdp_read_text",
        "optix_cdp_find_text", "optix_cdp_diff", "optix_cdp_click",
        "optix_cdp_fill", "optix_cdp_type", "optix_cdp_key",
        "optix_cdp_navigate",
    )
    _legacy_enabled = os.environ.get("FTXMCP_LEGACY_TOOLS") == "1"
    for _alias in _CDP_ALIASES:
        _tool = mcp._tool_manager._tools.get(_alias)
        if _tool is None:
            continue
        if _legacy_enabled:
            _tool.description = (
                "(deprecated: use optix_observe/optix_interact) "
                + (_tool.description or "")
            )
        else:
            mcp._tool_manager._tools.pop(_alias, None)

    # FTXMCP_SKILLS=0 drops the skill-catalog tools entirely (optix_list_skills /
    # optix_get_skill), forcing a self-evident-tools-only surface. Default keeps
    # them. This is the A/B lever for "do the tools alone suffice?" — and it
    # removes the reflexive skill-pull the agent does even when the instructions
    # say skills are reactive reference (observed pulling 6 skills unprompted).
    if os.environ.get("FTXMCP_SKILLS") == "0":
        for _sk in ("optix_list_skills", "optix_get_skill"):
            mcp._tool_manager._tools.pop(_sk, None)

    # FTXMCP_BRIDGE_PRIMITIVES=1 restores the 16 per-noun bridge primitives
    # (set_property/bind_property/attach_expression/attach_formatter/
    # wire_event/delete_node/
    # move_node/reorder/create_variable/create_folder/create_object/
    # create_type/create_alias/create_widget/add_translation). Each is 1:1
    # with an optix_bridge_edit op verb, so by DEFAULT they are popped —
    # opposite polarity from FTXMCP_SKILLS above (default OFF here, not
    # default ON): they clutter the surface for no capability optix_bridge_edit
    # doesn't already cover (a one-op list is a single edit). The composite
    # wrappers (add_label / add_bound_widget / add_navigation_panel_item),
    # optix_bridge_convert_to_type, optix_bridge_ensure_web_engine,
    # optix_bridge_validate_expression, optix_bridge_status,
    # optix_bridge_invoke_method (no optix_bridge_edit op verb
    # invokes an arbitrary NetLogic method), and optix_bridge_edit itself are
    # NEVER gated -- they either have no optix_bridge_edit-op equivalent or
    # ARE the batch tool. Any other value
    # (unset / "0" / anything != "1") keeps them popped, mirroring the
    # FTXMCP_LEGACY_TOOLS gate's "1" opt-in shape above.
    _BRIDGE_PRIMITIVES = (
        "optix_bridge_set_property", "optix_bridge_bind_property",
        "optix_bridge_attach_expression", "optix_bridge_attach_formatter",
        "optix_bridge_wire_event",
        "optix_bridge_delete_node", "optix_bridge_move_node",
        "optix_bridge_reorder", "optix_bridge_create_variable",
        "optix_bridge_create_folder", "optix_bridge_create_object",
        "optix_bridge_create_type", "optix_bridge_create_alias",
        "optix_bridge_create_widget", "optix_bridge_add_translation",
        "optix_bridge_create_netlogic",
    )
    if os.environ.get("FTXMCP_BRIDGE_PRIMITIVES") != "1":
        for _prim in _BRIDGE_PRIMITIVES:
            mcp._tool_manager._tools.pop(_prim, None)

    # Keep the asyncio event loop free. FastMCP runs a SYNCHRONOUS tool fn
    # directly on the loop, so any tool doing BLOCKING I/O -- a subprocess
    # shell-out, a urllib bridge call, a large file read -- stalls the loop for
    # its whole duration, and while stalled uvicorn cannot accept new
    # connections. So offload by DEFAULT and keep only a denylist on the loop.
    #
    # This inverts the previous _OFFLOAD_TOOLS allowlist. "read-only" is NOT
    # "non-blocking": the bridge READ tools do blocking HTTP and MUST offload
    # too -- keeping them on the loop was the original bridge-drop bug, and an
    # allowlist silently mis-classified every newly added tool.
    #
    # Thread-pool limiter — root-cause A hardening (2026-09-05 incident).
    # Evidence from service.jsonl on a test box: four consecutive "start" entries with
    # no following "stop" or "crash" — the external_kill signature, meaning the
    # process was killed externally by the Task Scheduler's ExecutionTimeLimit
    # (result 0x00041306) rather than dying with a traceback.  A 3450-op burst
    # (2988 template-build + 462 screen-emit) can saturate the default anyio
    # thread pool: without a limiter, each offloaded call spins its own thread
    # and the pool grows until the process stalls and the scheduler fires.  An
    # explicit CapacityLimiter makes the bound part of the service config and
    # prevents the stall → external-kill cycle.
    #
    # Ruled-out branches (cite the log lines that exclude them):
    #   B — Unhandled exception → crash + traceback in service.jsonl.
    #       The doctor output showed only external_kill rows (no "crash"
    #       event, no exc_type, no first_frame) — hypothesis B is ruled out.
    #   C — Batch-journal memory growth (_write_batch_journal rewrites JSON
    #       after every op).  Predicts neither external-kill nor crash cleanly;
    #       requires measuring process RSS across a 3000-op synthetic batch
    #       before acting.  The absence of "crash" entries and the external-kill
    #       pattern point away from OOM — C is ruled out pending measurement.
    #
    # OPTIX_THREAD_LIMITER sizes the cap; default 40.
    _thread_limit = int(os.environ.get("OPTIX_THREAD_LIMITER", "40"))
    _limiter = anyio.CapacityLimiter(_thread_limit)

    _STAY_SYNC = frozenset(("optix_health", "optix_list_projects"))
    for _name, _tool in mcp._tool_manager._tools.items():
        if _tool.is_async or _name in _STAY_SYNC:
            continue
        _sync_fn = _tool.fn

        async def _offloaded(*a, _sync_fn=_sync_fn, _limiter=_limiter, **k):
            return await anyio.to_thread.run_sync(
                functools.partial(_sync_fn, *a, **k), limiter=_limiter
            )

        _offloaded.__name__ = getattr(_sync_fn, "__name__", "tool")
        _offloaded.__doc__ = _sync_fn.__doc__
        # Original sync fn kept reachable for tests that call the tool
        # surface directly without an event loop.
        _tool._ftx_sync_fn = _sync_fn
        _tool.fn = _offloaded
        _tool.is_async = True

    # Per-call traffic stats (state_dir/logs/traffic.jsonl): wrap the
    # ToolManager dispatch so every MCP tool call records name, request/
    # response character sizes, duration and outcome — sizes only, never
    # content. FastMCP.call_tool resolves self._tool_manager.call_tool at
    # call time, so wrapping the instance attribute covers every tool
    # without touching the registrations above.
    _dispatch = mcp._tool_manager.call_tool

    async def _measured_dispatch(name, arguments, *args, **kwargs):
        t0 = time.monotonic()
        try:
            chars_in = len(json.dumps(arguments, default=str)) if arguments else 0
        except Exception:
            chars_in = 0
        try:
            # Per-tool scope refinement (the check auth.DEFAULT_SCOPE_RULES
            # defers here): the /mcp transport only requires `read`, so without
            # this a `read` token could drive every write/destructive tool. Only
            # engages when the request was token-authenticated; the auth-off
            # loopback default carries no token scope and is unaffected.
            token_scope = _authenticated_token_scope()
            if token_scope is not None:
                required = _required_tool_scope(mcp, name)
                try:
                    allowed = auth.scope_satisfies(token_scope, required)
                except ValueError:
                    allowed = False
                if not allowed:
                    raise ScopeInsufficient(
                        f"token scope {token_scope!r} cannot call {name!r} "
                        f"(requires {required!r}); re-issue with a higher scope "
                        "(deploy superset of author superset of read superset of health)"
                    )
            result = await _dispatch(name, arguments, *args, **kwargs)
        except Exception:
            core.traffic(cfg, tool=name, chars_in=chars_in, chars_out=0,
                         ms=int((time.monotonic() - t0) * 1000), ok=False)
            raise
        try:
            chars_out = len(json.dumps(result, default=str))
        except Exception:
            chars_out = 0
        core.traffic(cfg, tool=name, chars_in=chars_in, chars_out=chars_out,
                     ms=int((time.monotonic() - t0) * 1000), ok=True)
        return result

    mcp._tool_manager.call_tool = _measured_dispatch


    return mcp
