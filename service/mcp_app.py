"""FastMCP thin wrapper over service.core (see docs/architecture.md).

Tool descriptions are a shipped UX surface.
Each docstring includes a "use when" and "do NOT use when" so an LLM-side
MCP client picks the right tool for the right reason.
"""
from __future__ import annotations

import json
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__, auth, core

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
    """Minimum token scope to invoke tool `name`: read-only tools need `read`,
    anything that mutates (write / destructive) needs `deploy`. This mirrors the
    HTTP route scopes (auth.DEFAULT_SCOPE_RULES) so the two surfaces cannot
    diverge — the very gap this closes. An unknown/annotation-less tool fails
    closed to `deploy` (most restrictive), matching resolve_required_scope."""
    tool = mcp._tool_manager._tools.get(name)
    ann = getattr(tool, "annotations", None)
    read_only = bool(getattr(ann, "readOnlyHint", False)) if ann is not None else False
    return "read" if read_only else "deploy"


def make_mcp(cfg: core.Config) -> FastMCP:
    mcp = FastMCP(
        "ftx-mcp",
        # Surfaced automatically to the assistant at connect time (the MCP
        # `instructions` field) — the always-visible orientation; full
        # playbooks load on demand via the skill tools. Keep this SHORT: it
        # lands in every connected session's context.
        instructions=(
            "ftx-mcp drives FactoryTalk Optix Studio on this machine: author "
            "HMI changes into the OPEN Studio project via the design-time "
            "bridge, preview in the built-in emulator, verify on the rendered "
            "canvas.\n"
            "START OF SESSION -- do this ONCE, before authoring or answering "
            "the first Optix request, not only when a task 'seems to need' "
            "it:\n"
            "  1. optix_get_project_map() -- you are blind to the project's "
            "screens, variables and structure until you call it; skipping it "
            "means guessing at names the map would have handed you.\n"
            "  2. optix_list_skills() -- the bundled authoring playbooks "
            "(navigation, bound controls, styles, expressions, alarms); scan "
            "the catalog so you know what proven recipe exists before you "
            "improvise.\n"
            "Then the authoring loop: optix_bridge_* (author) -> "
            "optix_restart_emulator (structural edits only become visible "
            "after a restart) -> optix_cdp_screenshot (verify; optix_cdp_fill "
            "types into fields). Pull one playbook with optix_get_skill(name) "
            "when the task matches it. Run optix_doctor first if anything "
            "seems broken."
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_health() -> dict:
        """Aggregate health of the ftx-mcp deploy stack (export-based, v0.2.x).

        Returns: projects_root, studio_exe, runtime_dir, runtime_test_port,
        interactive_session, bind config. The deploy mechanism is
        Studio-export -> atomic tree swap -> runtime bounce; UpdateSvc /
        OPC UA is NOT in the path until v0.3.

        Use this when:
          - the user asks "is everything wired up?", "is studio installed?"
          - before a deploy attempt, to fail fast on missing studio_exe /
            unconfigured runtime_dir / non-interactive session
          - confirming the runtime tree the deploy will swap into is on the
            expected path

        Do NOT use this when:
          - the user wants live runtime liveness of a specific slot
            (use optix_runtime_status)
          - you only need the Studio binary version (use optix_studio_version)
        """
        return core.health(cfg)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_doctor() -> dict:
        """One-call setup check: every prerequisite + a plain-English fix for each.

        Returns {ready, checks:[{name, ok, required, detail, fix}]}. `ready` is True
        when the REQUIRED deps (Studio, projects folder) are present; feature checks
        (bridge, cdp, deploy account/cert/password, interactive session) are
        reported with a fix and gate only their own feature. Run this first on a new
        box, or whenever something "doesn't work" — it tells you exactly what's
        missing and how to fix it, in plain language.

        Use this when:
          - first-time setup, or after a reboot / config change
          - any tool failed and you want to know which dependency is missing

        Do NOT use this when:
          - you need live service metrics (use optix_health / optix_services_status)
          - you already know the specific failure (go straight to the relevant tool)
        """
        return core.doctor(cfg)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_list_skills() -> dict:
        """Catalog of the bundled authoring playbooks — one line each.

        The playbooks encode the proven recipes (multi-screen navigation,
        bound controls, styles, computed expressions, anchoring, alarms) with
        the exact property names and gotchas. Scan the catalog when a task
        matches a common pattern, then optix_get_skill(name) for the one you
        need — don't load all of them.

        Use this when:
          - starting a task that smells like a common HMI pattern
          - unsure whether a playbook exists for what you're building

        Do NOT use this when:
          - you already know the skill name (optix_get_skill directly)
        """
        return core.list_skills(cfg)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_get_skill(name: str) -> dict:
        """Full content of one bundled playbook by name (from optix_list_skills).

        Returns {name, content} — follow the playbook's steps with the bridge
        tools. Ships with the server, so it can never drift from the tool
        surface it describes.

        Use this when:
          - the task matches a playbook from the catalog

        Do NOT use this when:
          - the task is simple enough that the tool docstrings already cover it
        """
        return core.get_skill(cfg, name)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_list_projects() -> dict:
        """List Optix projects under OPTIX_PROJECTS_ROOT.

        Returns {projects: [{name, optix_file}, ...]}. You RARELY need this:
        every project-scoped tool defaults to the project the bridge is
        serving (the one open in Studio) when `project` is omitted.

        Use this when:
          - the user asks "what projects are on the box?"
          - you're targeting a DIFFERENT project than the one open in Studio
            and need its exact name
          - no bridge is running and a tool returned no_project

        Do NOT use this when:
          - you're working with the project open in Studio — just omit
            `project` on the other tools; do not enumerate first
          - you already know the project name (skip ahead)
        """
        return {"projects": core.list_projects(cfg)}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_find(
        query: str,
        glob: str = "**/*",
        max_results: int = 200,
        context_lines: int = 2,
        case_sensitive: bool = False,
        project: str | None = None,
    ) -> dict:
        """Search a project's text files for a literal string — server-side.

        THE discovery primitive: use it to locate which file and line hold a
        screen, widget, node name, or property before reading or editing.
        Returns {matches: [{path, line, text, context_before, context_after}],
        files_scanned, match_count, truncated}. Skips .git/bin/obj and
        binary files. Case-insensitive by default. Literal only — no regex,
        single-line queries only.

        Refuses with `studio_open` (409) while FactoryTalk Optix Studio is
        running (disk state is stale while Studio holds a project in memory;
        close Studio, no override exists).

        Use this when:
          - you need to find which Nodes/*.yaml defines a screen or widget
            (e.g. query="Name: Screen1" or query="Type: Label")
          - you want the line number to anchor a ranged optix_read_file
          - you would otherwise read whole files hunting for one node

        Do NOT use this when:
          - you already know the file AND region (ranged optix_read_file)
          - you need multi-line matching (read the file instead)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.find_in_project(
            cfg,
            project,
            query,
            glob=glob,
            max_results=max_results,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_read_file(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        project: str | None = None,
    ) -> dict:
        """Read a UTF-8 file under an Optix project, optionally a line range.

        Path-traversal is rejected. Returns {path, size, sha256, total_lines,
        content} plus {start_line, end_line} when ranged. start_line/end_line
        are 1-based inclusive (end clamps to EOF). `size`, `sha256`, and
        `total_lines` ALWAYS describe the whole file — sha256 is the version
        fingerprint to cite when composing anchored edits.

        Refuses with `studio_open` (409) while FactoryTalk Optix Studio is
        running anywhere on this box: Studio holds the open project in
        memory, so disk state is stale and any edit planned from it would be
        wrong. Tell the user to close Studio, then retry. There is no
        override parameter — do not look for one.

        Use this when:
          - you need the current content of a YAML/screen file before editing
          - optix_find located the region and you want just that slice
            (start_line/end_line) instead of a 2,000-line file
          - the user asks "what's in <file>?"

        Do NOT use this when:
          - you don't know which file holds the node (optix_find first)
          - the file is binary (returns a 415-equivalent error)
          - you want to list a directory (NOT supported)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.read_file(
            cfg, project, path, start_line=start_line, end_line=end_line
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_deploy(
        edits: list[dict],
        commit_message: str = "Automated edit",
        run_after_deploy: bool = True,
        project: str | None = None,
    ) -> dict:
        """Apply edits and deploy via FTOptixStudio export -> tree swap -> runtime bounce (v0.2.x).

        *** REQUIRES FACTORYTALK OPTIX STUDIO CLOSED. *** This export path refuses
        with 409 studio_open while FTOptixStudio.exe is running, because exporting
        conflicts with Studio's open project (corruption guard). If Studio is OPEN
        with the design-time bridge armed (the live author->save->deploy->verify
        loop), do NOT use this — use optix_deploy_updatesvc, which deploys via the
        UpdateSvc without closing Studio. There is no close-Studio tool; either close
        it yourself or switch to optix_deploy_updatesvc.

        Mechanism: applies `edits` to the project tree, git-commits with
        `commit_message`, runs `FTOptixStudio.exe export --platform=Win32_x64`
        to produce a runtime-ready bundle, atomically swaps the bundle into
        OPTIX_RUNTIME_DIR/<project>/, stops the runtime, restarts it via
        OPTIX_RUNTIME_LAUNCHER, and probes the runtime port until it
        answers. UpdateSvc / OPC UA is NOT in the path; see docs/architecture.md
        contract for the v0.3 future.

        Args:
          project: project directory name. Omit it — defaults to the bridge's
            served project; only pass it (or discover via optix_list_projects)
            when targeting a project OTHER than the one open in Studio.
          edits: list of edit dicts, UTF-8 plaintext only. THREE MODES —
            prefer the anchored modes; only fall back to full-content for
            new files or full rewrites:

            1. {"path", "find", "replace", "expect_count"?} — ANCHORED
               REPLACE (preferred for changing existing lines/values).
               `find` is a literal string that must occur exactly
               expect_count times (default 1) or the whole batch refuses
               (422 edit_anchor_mismatch) with nothing written. Newlines in
               find/replace auto-match the file's EOL (write "\\n"; CRLF
               files just work).
            2. {"path", "insert_after_anchor", "block"} — ANCHORED INSERT
               (preferred for adding nodes/widgets). `block` is inserted on
               a new line after the line containing the unique anchor.
               Indent the block yourself to match its destination.
            3. {"path", "content"} — FULL REPLACE. Only mode that can
               create a new file. For existing files, prefer modes 1-2:
               re-emitting a whole file risks drifting unrelated lines.

            The batch is atomic: ALL edits resolve against current disk
            state before ANY file is written; one mismatch refuses all.
            One edit per path per batch (later anchored edits would not see
            earlier ones).
          commit_message: git commit message for the audit trail
          run_after_deploy: when True, the runtime is bounced and verified
            via runtime_probe (TCP probe of the test port). When False,
            the swap completes but the runtime is not bounced; verification
            falls back to export_mtime (the swapped tree's mtime advanced
            past deploy start). False is for staged deploys where another
            agent flips the runtime separately.

        Returns the deploy-contract result (docs/architecture.md):
          state ∈ {succeeded, failed},
          studio_exit, verification.{method, confirmed_at, timeout_seconds},
          started_at, completed_at, git_sha, files_written,
          edit_summary: [{path, mode, occurrences?, bytes_before, bytes_after}].

          method ∈ {runtime_probe, export_mtime, null}; null only on a
          pre-verify failure (export non-zero or swap aborted).

        Refuses with `studio_open` (409) while FactoryTalk Optix Studio is
        running anywhere on this box — Studio's in-memory model stomps
        file-level edits on save/close (this corrupted a live demo; the
        guard is deliberate). `editor_project_open` (409) means VS / VS Code
        has THIS project open. Remediation in both cases is closing the
        app; there is no override parameter — do not look for one.

        Use this when:
          - the user wants to apply specific edits to an Optix project and
            push the change to the local runtime
          - you have located the target via optix_find / optix_read_file and
            can anchor the change (modes 1-2)

        Do NOT use this when:
          - FactoryTalk Optix Studio is open (close it first — see above)
          - you have not read the target region (optix_find -> ranged
            optix_read_file -> anchored edit is the canonical loop; blind
            full-content writes risk clobbering unrelated changes)
          - the edit is large or speculative; review first, then anchor
          - you only want to stage a build (use optix_studio_export — it
            produces the same staging tree without touching the live runtime)

        Verify-half tip: the returned verification.method is runtime_probe
        (TCP open) — that proves the runtime is up, NOT that your edit is
        visible on screen. For visual confirmation pair with
        optix_cdp_screenshot (server-side JPEG of the canvas).
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_list_screens(project: str | None = None) -> dict:
        """List the Screen / Panel / Dialog nodes in a project's UI.

        Returns {screens: [...], count, source}. `source` is "bridge" when the
        answer came from the live model (Studio open with this project + the
        design-time bridge running) or "file" when it came from the on-disk YAML.
        The starting point for any UI edit — you need the screen name (and, in
        file mode, the file it lives in) before adding a widget.

        Mode: when Studio is open with this project AND the design-time bridge is
        up, this reads the LIVE model (no refusal). Otherwise the file path runs,
        which still refuses with `studio_open` (409) if Studio is open with no
        bridge serving this project. There is no override either way.

        Use this when:
          - the user says "add X to the main screen" — find which screen that is
          - you need a screen's exact node name for optix_add_widget

        Do NOT use this when:
          - you already know the screen name AND file
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.list_screens(cfg, project)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_get_project_map(
        path: str | None = None, depth: int | None = None,
        max_nodes: int = 800, ids: bool = False, match: str | None = None,
        format: str = "outline", project: str | None = None,
    ) -> str | dict:
        """Component map of the project (names + nesting) in ONE call — the
        cheap way to learn a project's structure instead of walking it with
        repeated optix_describe_node.

        Depth is AUTOMATIC by what you point it at: a FOLDER (or no path) gives
        orientation ("overview") — folders expand, each component is one line
        with a "(+N inside)" count, variables fold into "(N vars)". A COMPONENT
        path (e.g. "UI/MainWindow") gives the full subtree ("detail" — every
        node kind, at the default depth 6; NOT necessarily fully expanded).
        Pass depth= to force a detail walk at that depth. Pointers and
        bindings are DEREFERENCED inline ("Panel (NodePointer -> UI/Screens/
        ScreenA)") — a detail map of a screen doubles as its wiring audit.
        Placeholder collections render as "Name {ElementType}"; truncation is
        always marked "... +N more".

        match="Pump*" turns the call into a LIVE-MODEL SEARCH (name or type,
        case-insensitive) returning matching full paths, ready to pass to any
        tool. MATCHING IS EXACT unless you use * wildcards: match="Grid" only
        finds nodes NAMED/TYPED exactly "Grid" — a node named GridLayout1 needs
        match="Grid*" or match="*Grid*". When hunting for something, default to
        "*substring*"; a bare word silently returns 0 hits for partial names
        (two independent field agents have mis-assumed substring semantics).
        Use this instead of optix_find for live-model lookups (optix_find greps
        FILES and is refused while Studio is open).

        IMPORTANT: the map shows MATERIALIZED nodes only. A property a type
        offers but that was never set does NOT appear — its absence here never
        means you can't set it (optix_bridge_set_property materializes on
        write). Consult optix_describe_type for what a type accepts.

        format="outline" (default) returns readable indented text with a
        header line — for HUMAN/model eyes. Programmatic callers should use
        format="json" (structured tree + mode/truncated fields; never parse
        the outline header). ids=true adds NodeIds (rarely needed — tools
        address nodes by PATH).

        Use this when:
          - starting work on an unfamiliar project (no-path overview first)
          - you need the exact path/nesting of components in one subtree
          - you'd otherwise call optix_describe_node more than twice

        Do NOT use this when:
          - you need one node's property VALUES (optix_describe_node)
          - you need what a TYPE accepts / settable schema (optix_describe_type)
          - Studio/the bridge is closed (bridge-only; arm the bridge first)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_bridge_status() -> dict:
        """Status of the design-time read-bridge (NetLogic HTTP listener in Studio).

        Returns {available, project, bridge_version, reason}. `available` is True
        only when the bridge answers /bridge/health and a project model is loaded;
        `project` is the project it is serving (its self-attribution — the OS-level
        Studio-open guard cannot name the open project, the bridge can).

        Use this when:
          - deciding whether live-model reads (optix_describe_node) will work, or
            whether you are in file-only mode
          - the user asks "is the bridge up / what project is open in Studio?"

        Do NOT use this when:
          - you just want to read a node — call optix_describe_node and handle the
            bridge_unavailable error instead of pre-checking
        """
        return core.bridge_state(cfg)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_describe_node(path: str, project: str | None = None) -> dict:
        """Introspect one node in the LIVE model via the design-time bridge.

        Returns {path, browse_name, node_class, dotnet_type, children:[{browse_name,
        node_class, dotnet_type}], properties:[{name, datatype, value}], truncated,
        source:"bridge"}. `path` is an Optix model path (NO leading slash), e.g.
        "UI/MainWindow", "UI/Screens/Overview", "Model/Motor1".

        This is typed, live-model introspection — it returns a node's real type and
        its property schema (Width/Height/Text/...) directly from Studio's in-memory
        model, with no YAML guessing. It REQUIRES Studio open with this project AND
        the bridge running; otherwise it raises `bridge_unavailable` (503) — there is
        no file-path equivalent for typed introspection. Check optix_bridge_status if
        unsure.

        Use this when:
          - you need a node's exact type or property schema before editing
          - you want to discover what a screen/panel contains (its children)
          - you would otherwise read raw YAML and guess the node shape

        Do NOT use this when:
          - Studio is closed (the bridge is down — use optix_find / optix_read_file
            against the files instead)
          - you need a full-text search (use optix_find)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.describe_node(cfg, project, path)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_list_ui_types(project: str | None = None) -> dict:
        """List the builtin UI type catalog from the LIVE model (design-time bridge).

        Returns {types:[{name, browse_name}], count, truncated, source:"bridge"}.
        This is "what controls exist?" — Label, Button, Rectangle, Panel, DataGrid,
        Trend, … — read from Studio's type system, not guessed. Bridge-only;
        requires Studio open with this project and the bridge running (else
        `bridge_unavailable`). Pair with optix_describe_type to get a type's
        property schema before composing an edit.

        Use this when:
          - you need to know which control types are available before adding one
          - the user asks "what widgets/controls can I use?"

        Do NOT use this when:
          - you already know the type name (go straight to optix_describe_type)
          - Studio is closed (the bridge is down)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.list_ui_types(cfg, project)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_describe_type(
        type_name: str | None = None, type_names: list[str] | None = None,
        project: str | None = None,
    ) -> dict:
        """Property schema of a builtin UI type from the LIVE model (design-time bridge).

        Returns {type, browse_name, properties:[{name, datatype}], truncated,
        source:"bridge"}. `type_name` is a catalog name from optix_list_ui_types
        (e.g. "Label", "Button", "Rectangle"). This is the "shape" of a control —
        which properties it has and their datatypes — so an edit can be composed
        against the real schema instead of guessed YAML. Bridge-only; requires
        Studio open with this project and the bridge running (else
        `bridge_unavailable`); `node_not_found` for an unknown type.

        Use this when:
          - before adding/setting a property on a control, to confirm the property
            exists and its datatype
          - the user asks "what properties does a <type> have?"

        Do NOT use this when:
          - you want the properties of a specific existing NODE (use
            optix_describe_node with its path)
          - Studio is closed (the bridge is down)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        if type_names:
            # batch form: one round trip for a type survey
            out: dict = {"schemas": [], "errors": []}
            for tn in type_names:
                try:
                    out["schemas"].append(core.describe_type(cfg, project, tn))
                except core.CoreError as e:
                    out["errors"].append({"type": tn, "error": str(e)})
            return out
        if not type_name:
            return {"error": "bad_request",
                    "message": "pass type_name or type_names"}
        return core.describe_type(cfg, project, type_name)

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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_create_widget(
        screen: str, name: str, widget_type: str = "Label",
        project: str | None = None,
    ) -> dict:
        """Create a UI widget on a screen in the LIVE model via the design-time bridge.

        Adds a builtin control (Label/Button/Rectangle/Panel/...) as a child of
        `screen` (an Optix path, NO leading slash, e.g. "UI/MainWindow"). Returns
        {ok, created_path, type, ...}.

        PLACEHOLDER-COLLECTION AUTO-ROUTING: some parents
        declare a named child collection (NavigationPanel.Panels,
        DataGrid.Columns, ListView.TypeSelectors, XYChart.Pens, gauges'
        WarningZones) — children belong INSIDE it, like Studio's drag-and-drop.
        Target the PARENT's path and the bridge routes automatically when the
        widget_type fits the collection: created_path/routed_into report the
        real placement (e.g. screen=".../NavPanel" + NavigationPanelItem lands
        at ".../NavPanel/Panels/<name>"). optix_describe_type marks these
        properties with children_go_in/element_type. Errors are loud:
        ambiguous_container (type fits several collections — pass the explicit
        sub-path) and read_only_collection (runtime-managed, e.g.
        Trend.TimeRanges — never authorable). A NavigationPanelItem renders a
        ZERO-WIDTH tab until its Title is set — set Title right after creating.

        This is a LIVE-MODEL write: Studio authors
        the node, so it is export-safe by construction (no file-injection export
        hang). Requires Studio open with this project AND the bridge running (else
        bridge_unavailable). To SEE it: a new widget is a STRUCTURAL change — a
        running emulator won't show it until a restart cycle
        (optix_stop_emulator -> optix_run_emulator; F5 saves, no explicit save
        needed). Ship from Studio's Deploy dialog when ready.

        Use this when:
          - adding a control while Studio is open (the live, export-safe path)
          - you've confirmed the type via optix_list_ui_types

        Do NOT use this when:
          - Studio is closed (live authoring needs Studio + the bridge)
          - you only want to author an edit without applying it
          - the plan is create-then-bind: use optix_bridge_add_bound_widget
            instead — it is TRANSACTIONAL (a failed bind rolls back the create,
            so no orphan widget is left on the screen; hand-rolling
            create_widget + bind_property has no such guarantee)
          - you want a Folder / plain Object / custom-type instance — those are
            structural nodes, not catalog widgets: optix_bridge_create_folder,
            optix_bridge_create_object (also instantiates custom types),
            optix_bridge_create_type
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_create_widget(
            cfg, project, screen, name, widget_type))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
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
        3-5 calls to build. Only the args you pass are applied; steps run in
        order and the composite is TRANSACTIONAL — a failure after creation
        rolls the created node back automatically ({ok: false, failed_step,
        rolled_back: true}), so a retry with the same name is always safe.
        Example: a Switch bound to a model flag:
        add_bound_widget(screen="UI/Screens/ScreenA", name="PumpSwitch",
        widget_type="Switch", left=40, top=60, bind_property="Checked",
        source_path="Model/PumpRun", mode="ReadWrite").

        Use this when:
          - adding any positioned and/or bound control (the common case)

        Do NOT use this when:
          - wiring EVENTS (optix_bridge_wire_event after creating)
          - attaching computed expressions (optix_bridge_attach_expression)
          - a plain static label (optix_bridge_add_label is one arg shorter)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_add_bound_widget(
            cfg, project, screen, name, widget_type, left=left, top=top,
            width=width, height=height, text=text,
            bind_property=bind_property, source_path=source_path, mode=mode))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_add_navigation_panel_item(
        panel_path: str, title: str, screen_path: str | None = None,
        name: str | None = None, project: str | None = None,
    ) -> dict:
        """Add a tab to a NavigationPanel in ONE call: create the item (auto-
        routed into Panels), set its Title, and point it at a screen.

        Title is REQUIRED because an empty-Title item renders a zero-width
        invisible tab. screen_path (e.g. "UI/Screens/ScreenD") wires what the
        tab shows; omit it to wire later via set_property "Panel". Restart the
        emulator to see the new tab (structural edit).

        Use this when:
          - adding a navigation tab (the create/Title/Panel trio in one call)

        Do NOT use this when:
          - reordering tabs (optix_bridge_reorder)
          - retitling an existing tab (optix_bridge_set_property "Title")
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_add_navigation_panel_item(
            cfg, project, panel_path, title, screen_path=screen_path, name=name))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_add_label(
        screen: str, name: str, text: str,
        left: float | None = None, top: float | None = None, locale: str = "en-US",
        project: str | None = None,
    ) -> dict:
        """Add a Label with text (+ optional position) in ONE call via the live bridge.

        The common "put a label on the screen" case in a single round-trip: creates a
        Label named `name` on `screen` (Optix path, no leading slash, e.g.
        "UI/MainWindow"), sets its Text, and — if given — LeftMargin/TopMargin.
        Equivalent to optix_bridge_create_widget + optix_bridge_set_property x1-3, but
        one tool call instead of four. Returns {ok, created_path, text, left, top}.
        Requires Studio open with this project AND the bridge running. After this,
        optix_save persists it; ship from Studio's Deploy dialog when ready.

        Use this when:
          - the user wants a label on a screen with some text (the everyday case)
          - you'd otherwise chain create_widget + several set_property calls

        Do NOT use this when:
          - Studio is closed (live authoring needs Studio + the bridge)
          - the widget isn't a Label (use optix_bridge_create_widget)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_add_label(
            cfg, project, screen, name, text, left=left, top=top, locale=locale))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_ensure_web_engine(
        port: int = 8081, ip: str = "0.0.0.0", project: str | None = None,
    ) -> dict:
        """Ensure a Web presentation engine exists so the runtime serves a canvas.

        Without a WebUIPresentationEngine under UI, a deployed runtime renders no
        web canvas (and CDP verify has nothing to screenshot) — the manual "add UI →
        Web presentation engine" setup step. Idempotent: returns {existed:true} if
        one is already present, else creates + configures one (Port, Protocol=HTTP,
        StartWindow → the first window) and returns {existed:false, path, port,
        start_window}. Requires Studio open with this project AND the bridge running.
        Run once during project setup, then author→save→deploy→verify has something
        to serve.

        Use this when:
          - setting up a new/scratch project for the deploy-verify loop
          - a deploy serves nothing / the CDP screenshot is blank (no web engine)

        Do NOT use this when:
          - Studio is closed (bridge-only; open Studio + StartBridge first)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_ensure_web_engine(
            cfg, project, port=port, ip=ip))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_set_property(
        node_path: str, name: str, value: str, locale: str = "en-US",
        project: str | None = None,
    ) -> dict:
        """Set a property on a LIVE-model node via the design-time bridge.

        `node_path` is an Optix path (NO leading slash), `name` the property (Text,
        Width, FontSize, ...), `value` a string coerced to the property's type.
        Returns {ok, datatype, via, value}. Materializes a freshly-created
        instance's inherited property (e.g. a new Label's Text) so it persists AND
        renders — the fix for the GetVariable-null trap. Requires Studio open with
        this project AND the bridge running (else bridge_unavailable). `locale`
        applies to LocalizedText props. To SEE it in a running emulator: property
        edits on existing widgets, like all Studio-side edits, need an emulator
        restart cycle (optix_stop_emulator -> optix_run_emulator) — the emulator
        renders its own loaded snapshot, not the live Studio model. Ship from
        Studio's Deploy dialog when ready.

        Use this when:
          - setting a property on a node while Studio is open
          - you just created a widget and need to set its Text/etc.

        Do NOT use this when:
          - Studio is closed (live authoring needs Studio + the bridge)
          - you need a keyed translation (a future i18n endpoint)
          - the property is ARRAY-typed (String[] like GridLayout.Columns/Rows,
            NodeId[] like NavigationPanelItem.AliasNodeArray — describe shows
            these with a "[]" suffix): array writes return
            unsupported_array_write; author array values in Studio directly.
          - you're DIAGNOSING why something doesn't render: read the current
            values with optix_describe_node instead of writing presumed
            defaults (Visible=true/Opacity=1) "to rule things out" — each such
            write MATERIALIZES the property into the project file permanently
            while changing nothing. See the optix-verify-loop skill's
            blank-render checklist.
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_set_property(
            cfg, project, node_path, name, value, locale))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_create_variable(
        name: str, parent: str = "Model", datatype: str = "Boolean",
        project: str | None = None,
    ) -> dict:
        """Create a model variable in the LIVE model via the design-time bridge.

        Adds a variable `name` of `datatype` (Boolean/Int32/Double/String) under
        `parent` (an Optix path, default "Model"). Returns {ok, created_path,
        datatype}. Live-model write (export-safe by construction, unlike the
        file-path bare-shape workaround). Requires Studio open with this project
        AND the bridge running. Persist with optix_save.

        Use this when:
          - adding a model variable while Studio is open
          - you'll bind a widget property to it next

        Do NOT use this when:
          - Studio is closed (live authoring needs Studio + the bridge)
          - you want a CONTAINER for variables (optix_bridge_create_object) or
            a grouping folder (optix_bridge_create_folder)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_create_variable(
            cfg, project, name, parent, datatype))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_create_folder(
        parent: str, name: str, project: str | None = None,
    ) -> dict:
        """Create a structural FOLDER in the LIVE model via the design-time bridge.

        Folders (OpcUa FolderType) are organizational nodes — Model subtrees,
        UI/Templates, grouping — NOT UI controls, which is why they aren't in
        optix_bridge_create_widget's catalog. `parent` is an Optix path
        ("Model", "UI"). Returns {ok, created_path}. Duplicate sibling names
        are refused loud (name_exists). Persist with optix_save.

        NOTE: Studio auto-promotes a widget dropped at the Templates ROOT into
        an ObjectType; the bridge does no promote-by-location magic — create
        types explicitly with optix_bridge_create_type.

        Use this when:
          - organizing Model/UI subtrees, creating a Templates folder
        Do NOT use this when:
          - you want a data-holding container (optix_bridge_create_object)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_create_folder(
            cfg, project, parent, name))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_create_object(
        parent: str, name: str, object_type: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Create a plain OBJECT container — or an INSTANCE of a custom type —
        in the LIVE model via the design-time bridge.

        With no `object_type`: a BaseObjectType container (structured model
        data — group variables under it with optix_bridge_create_variable).
        With `object_type` = a path to a project ObjectType (e.g.
        "UI/Templates/PumpCard"): creates an INSTANCE of that type — this is
        how you reuse a template made with optix_bridge_create_type or
        optix_bridge_convert_to_type. Passing an instance path errors
        not_a_type. Returns {ok, created_path, type, node_class}.

        Use this when:
          - structuring model data (Motor1 with Speed/Power/Running under it)
          - instantiating a custom template type onto a screen or into Model
        Do NOT use this when:
          - you want a builtin UI control (optix_bridge_create_widget)
          - you want a plain grouping node (optix_bridge_create_folder)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_create_object(
            cfg, project, parent, name, object_type))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_create_type(
        name: str, parent: str, base_type: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Create an OBJECT TYPE (reusable template) in the LIVE model.

        `base_type` is a builtin UI type name (RowLayout, Button — see
        optix_list_ui_types) so the type renders like its base, OR a path to
        another project ObjectType (subtyping), OR omitted for a bare
        model-side structured type. Author the template's CONTENT by targeting
        the new type's path with the normal tools (create_widget/set_property/
        bind_property write into types exactly like into MainWindow — which IS
        a WindowType). Instantiate with optix_bridge_create_object
        (object_type=<this type's path>). Returns {ok, created_path, base}.

        PLAN-AHEAD workflow: create the type FIRST, author inside it, then
        instantiate everywhere. To promote an already-built instance instead,
        use optix_bridge_convert_to_type.

        Use this when:
          - building a reusable widget/template before any instance exists
          - defining structured model types (MotorType with Speed/Power)
        Do NOT use this when:
          - a one-off widget is enough (optix_bridge_create_widget)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_create_type(
            cfg, project, name, parent, base_type))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_bridge_move_node(
        node_path: str, new_parent: str, new_name: str | None = None,
        project: str | None = None,
    ) -> dict:
        """MOVE (reparent) a live instance to a new parent — e.g. an existing
        column of widgets into a freshly-created ScrollView.

        Implemented as re-authoring (copy under the new parent with link
        fixups, then delete the original) — a raw node-model reparent corrupts
        the live model. Consequences to read in the response: the node gets a
        NEW NodeId, so INBOUND references from elsewhere to the moved subtree
        are NOT rewritten (outbound bindings ARE re-created — relative/alias
        raws verbatim, absolute links re-resolved, intra-subtree links remapped
        to the copy). `skipped` lists anything not copied (converters).
        optix_save first; render-verify after (structural change — restart the
        emulator).

        Use this when:
          - restructuring a screen (wrapping content in a new container)
          - a widget was created under the wrong parent
        Do NOT use this when:
          - other widgets bind INTO the subtree being moved (their links break
            — rebind after, or restructure around it)
          - you want a reusable template (optix_bridge_convert_to_type)
          - you only want z-order (optix_bridge_reorder)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_move_node(
            cfg, project, node_path, new_parent, new_name))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_bridge_convert_to_type(
        node_path: str, type_name: str, types_folder: str = "UI/Templates",
        replace: bool = True, project: str | None = None,
    ) -> dict:
        """Convert a LIVE instance into a reusable ObjectType — Studio's
        right-click "Convert to Type" refactor, which has no public API.

        Creates `type_name` (subtyping the instance's own type, so it keeps
        rendering/behavior) in `types_folder` (must exist —
        optix_bridge_create_folder first), RE-AUTHORS a copy of the subtree
        into it (fresh nodes born in the type, raw values copied, DynamicLinks
        re-created against their resolved targets — live children are never
        re-parented; that corrupted the model), and with replace=true (default)
        swaps the original for an instance of the new type. Returns {ok,
        type_path, copied_nodes, skipped, replaced, instance_path,
        links_verified, relative_links_unverified, broken_links, steps}.

        READ `skipped` — constructs the copy can't reproduce (expression
        converters, exotic attachments, unresolvable link targets) are listed
        there, not silently half-copied; re-attach those on the type by hand
        (optix_bridge_attach_expression etc.). Verify links via
        links_verified/broken_links. optix_save first is cheap insurance;
        render-verify the replacement instance after (structural change —
        restart the emulator).

        Use this when:
          - an already-built widget assembly should become a template
        Do NOT use this when:
          - nothing is built yet (optix_bridge_create_type + author into it)
          - the node is already an ObjectType (already_a_type)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_convert_to_type(
            cfg, project, node_path, type_name, types_folder, replace))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_bind_property(
        node_path: str, name: str, source_path: str | None = None,
        mode: str = "Read", raw_path: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Bind a node's property to a model variable (DynamicLink) via the bridge.

        Creates a live dynamic link so `node_path`.`name` tracks the model variable
        at `source_path`. `mode` in {Read, Write, ReadWrite}. This is the semantic
        "bind the label's Text to the status variable" operation (vs a static set).
        Requires Studio open with this project + the bridge running. Persist with
        optix_save.

        ALIAS/TEMPLATE binding uses `raw_path` INSTEAD of source_path: a literal
        NodePath like "{Alias1}/MyInt" (brace form, from the widget's owner) or
        "../../Alias1/MyInt" (owner-relative) that resolves PER INSTANCE at
        runtime — deliberately NOT resolvable at bind time; that late binding is
        what makes a template reusable. A source_path THROUGH an alias will
        always fail source_not_variable — that's the signal to switch to
        raw_path. No validation is possible on raw paths: render-verify after.

        Use this when:
          - wiring a UI property to live data (the engineering-grade authoring op)
          - the user says "bind/link this to that variable"
          - binding template widgets through an alias slot (raw_path)

        Do NOT use this when:
          - you just want a static value (use optix_bridge_set_property)
          - Studio is closed
          - the widget doesn't exist yet: use optix_bridge_add_bound_widget
            for create+bind in one TRANSACTIONAL call — if this bind fails
            after a separate create_widget, the orphan widget stays on the
            screen; the composite rolls it back automatically
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_bind_property(
            cfg, project, node_path, name, source_path, mode, raw_path))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_create_alias(
        parent_path: str, name: str, target_path: str | None = None,
        kind: str | None = None, project: str | None = None,
    ) -> dict:
        """Create an alias under a node — the parameter slot of a reusable
        component/template.

        `kind` sets the TYPE CONSTRAINT Studio's "+ Alias" carries (a builtin
        type name like "BaseObject"/"Motor", or a path to a project type node) —
        without it the alias is a bare NodeId pointer with no shape for
        validation. `target_path` is OPTIONAL and usually ABSENT on a template:
        each INSTANCE points the alias somewhere via
        optix_bridge_set_property(<instance>/<alias>, name="Value",
        value=<target path>). Bind the template's widgets THROUGH the alias
        with optix_bridge_bind_property(raw_path="{<name>}/<child>").
        Requires Studio open + the bridge. Persist with optix_save.

        Use this when:
          - adding a parameter/data slot to a template type (create_type /
            convert_to_type output)
          - making a widget reusable by aliasing its data target

        Do NOT use this when:
          - a plain dynamic link to a fixed variable suffices
            (optix_bridge_bind_property with source_path)
          - Studio is closed
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_create_alias(
            cfg, project, parent_path, name, target_path, kind))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
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
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_add_translation(
            cfg, project, key, value, locale))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
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
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_delete_node(
            cfg, project, node_path))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_reorder(
        node_path: str,
        position: str | None = None, index: int | None = None,
        project: str | None = None,
    ) -> dict:
        """Change a node's z-order among its siblings via the bridge.

        Render order = child order: the LAST child renders in FRONT, the first
        renders BEHIND. Pass `position="front"` (bring to front / on top) or
        `position="back"` (send behind everything), or an explicit `index`. This is
        the enabler for a Panel background: create a Rectangle, then send it to back
        so it sits behind the panel's other children. Live-model op; requires Studio
        open + the bridge. Persist with optix_save.

        Caveat: MoveUp/MoveDown only take effect on graphic objects inside a TYPE
        (ScreenType/PanelType) — the standard case for screen content. Reload the
        runtime page to see the visual change.

        Use this when:
          - a background rectangle needs to go behind existing widgets
          - bringing a control in front of / behind overlapping widgets

        Do NOT use this when:
          - the node is NOT inside a ScreenType/PanelType (MoveUp/Down no-ops
            outside a type — reorder silently has no effect)
          - Studio is closed (no live model to reorder)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_reorder_node(
            cfg, project, node_path, position=position, index=index))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_attach_expression(
        node_path: str, prop_name: str,
        expression: str, sources: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Attach an ExpressionEvaluator converter to a property via the bridge.

        The ExpressionEvaluator is FT Optix's formula language — "a dumb Excel with
        fewer functions" — and it subsumes ConditionalConverter, LinearConverter,
        etc. `expression` uses `{0}`,`{1}`,... placeholders bound in order to
        `sources` (comma-separated model/node paths). Functions: max/min/avg/abs/
        trunc/ceil/floor/round/sqrt/sign/like/isempty/`if`/left_of/right_of. Colors
        are `0xAARRGGBB`. Examples:
          - conditional color: expression="if({0} > 40, 0xFFFF0000, 0xFF00FF00)",
            sources="Model/Speed", on a widget's FillColor prop
          - computed visibility: expression="{0} && {1}", sources="Model/A,Model/B",
            on Visible
        Live-model op; requires Studio open + the bridge. Persist with optix_save.
        IMPORTANT: a converter no-ops SILENTLY if mis-wired — verify at runtime
        (deploy + screenshot), not just the {ok:true} return.

        Use this when:
          - a property must be COMPUTED from one or more sources (conditional
            color, computed visibility, scaling) — not a straight 1:1 bind
          - you'd otherwise reach for a ConditionalConverter/LinearConverter

        Do NOT use this when:
          - the property just mirrors ONE source 1:1 (use optix_bridge_bind_property)
          - the logic exceeds the 17-function set (needs a custom C# converter)
          - Studio is closed
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_attach_expression(
            cfg, project, node_path, prop_name, expression, sources=sources))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_bridge_validate_expression(
        expression: str, sources: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Syntax-check an ExpressionEvaluator formula WITHOUT attaching it.

        Optix validates a formula only at RUNTIME, where a bad one silently no-ops
        (the classic converter trap). This catches the common author-time mistakes up
        front: unbalanced ()/{}, a placeholder {N} beyond the number of sources, an
        unknown function name, an unterminated string. Returns {valid, sources,
        error?}. The SAME check gates optix_bridge_attach_expression, so a malformed
        formula is rejected there too — this tool is for checking BEFORE you commit.

        Use this when:
          - drafting a non-trivial formula and you want it verified before wiring
          - debugging why a converter renders nothing (validate the expression first)

        Do NOT use this when:
          - the expression is a plain 1:1 bind (use optix_bridge_bind_property)
          - Studio/the bridge is closed
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_validate_expression(
            cfg, project, expression, sources=sources))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_bridge_wire_event(
        node_path: str, event_type: str,
        method_path: str | None = None, command: str | None = None,
        variable: str | None = None, value: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Wire a UI event on a node — to a NATIVE command or a NetLogic method.

        Builds an EventHandler so `event_type` (e.g. MouseClickEvent) on `node_path`
        fires an action. Prefer a NATIVE command (no custom NetLogic): set
        command="SetVariable" with variable=<path> + value=<v>, or
        command="ToggleVariable" with variable=<path> — these wire to FT Optix's
        builtin VariableCommands. For custom logic, pass method_path
        ("ObjectPath/MethodName") to a NetLogic [ExportMethod]. Requires Studio open +
        the bridge; persist with optix_save; verify the runtime fires it after deploy.

        Use this when:
          - a button should set/toggle a variable (native command — no NetLogic)
          - a control should trigger a NetLogic [ExportMethod] (method_path)

        Do NOT use this when:
          - the event type isn't a builtin UI event (returns event_not_found)
          - Studio is closed
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return _bridge_guarded(project, lambda: core.bridge_wire_event(
            cfg, project, node_path, event_type, method_path,
            command=command, variable=variable, value=value,
        ))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_save(project: str | None = None) -> dict:
        """Persist the open project to disk (sends Ctrl+S to Studio).

        Studio has no programmatic save API (it is native C++/Qt), so this is the
        autonomous save: it focuses the project's Studio window and sends ^s, then
        verifies by watching the project's node-YAML mtime advance. Returns
        {saved, mtime_before, mtime_after, focused, elapsed_seconds}. Call this
        AFTER bridge authoring (optix_bridge_* writes the live model in RAM) when
        you need the edit ON DISK without running anything (e.g. to read the
        YAML back). Requires the service to run in an interactive session and
        the project open in Studio.

        You usually DON'T need this: optix_run_emulator's F5 saves as part of
        staging. And saving does
        NOT push edits into an already-RUNNING emulator — that's a separate
        process with its own loaded snapshot; structural changes need
        optix_stop_emulator -> optix_run_emulator to become visible.

        Use this when:
          - you need bridge edits on disk to read/verify the YAML (no run needed)
          - closing the live-model -> disk gap without starting the emulator

        Do NOT use this when:
          - you're about to optix_run_emulator anyway
            (F5 handles saving; an extra ^s is a redundant focus steal)
          - you expect it to refresh a running emulator (it can't — restart it)
          - Studio is closed (there's nothing to save; edit files directly)
          - you authored via file-path tools (those already write disk)
        """
        project = project or core.default_project(cfg)
        if not project:
            return {"saved": False, "error": "no project given and no bridge serving one — pass project or start the bridge"}
        return core.save(cfg, project)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_run_emulator(project: str | None = None, save_first: bool = False) -> dict:
        """Launch the project in Studio's built-in emulator (sends F5 to Studio).

        THE default verify step: use this FIRST for any preview/verify iteration —
        it's much cheaper and faster than a deploy. F5 is Studio's "start" button;
        it stages the in-Studio model (F5 saves as part of staging — no explicit
        save needed, save_first defaults to False) and spins up a LOCAL
        FTOptixRuntime. Shipping to hardware is a deliberate, human step from
        Studio's Deploy dialog after the emulator + optix_cdp_screenshot
        confirm the change.

        IMPORTANT — a RUNNING emulator does not pick up Studio edits. It is a
        separate process with its own loaded snapshot. Structural changes (new
        widgets, new bindings, size/layout) need a restart cycle:
        optix_stop_emulator -> optix_run_emulator (no save in between — F5 saves).
        Only already-on-screen interactive state (switches, spinboxes, text
        fields — things a user could click/type) can be exercised live without a
        restart. If a screenshot doesn't show your edit, restart the emulator
        before concluding the edit failed.

        F5 TOGGLES: check optix_emulator_status first so a blind "run" doesn't
        stop a running emulator. Returns {launched, focused, saved, serving} —
        serving=True means the runtime port answered (safe to screenshot).

        TARGET GUARD: F5 runs Studio's SELECTED deployment target. If Studio's
        dropdown has a non-emulator target active, this refuses
        (active_target_not_emulator) instead of pressing F5 — ask the user to
        switch the dropdown; the service never changes it. After launch the
        process identity is re-checked (runtime_identity), and a port that
        answers without an emulator process raises a warning. If the result is
        launched:true but runtime_identity:"not_running" with
        probable_cause:"target_or_modal", the dropdown was pointed elsewhere or
        a modal dialog ate the keystroke — surface that to the user and do NOT
        retry-loop this tool.

        Use this when:
          - verifying ANY bridge edit (the default fast loop)
          - iterating design-time (emulator restarts are cheap)

        Do NOT use this when:
          - the emulator is already running and you changed structure (stop it
            first — F5 on a running emulator STOPS it)
          - you're ready to ship to the real target (Studio's Deploy dialog)
          - Studio is closed (F5 has no target)
        """
        project = project or core.default_project(cfg)
        if not project:
            return {"launched": False, "error": "no project given and no bridge serving one — pass project or start the bridge"}
        return core.run_emulator(cfg, project, save_first=save_first)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_emulator_status() -> dict:
        """Emulator state: not_running / starting / running.

        F5 (optix_run_emulator) TOGGLES the Studio emulator, so a blind "run" can
        actually STOP a running one. Check this first. Counts ONLY real emulator
        processes (Studio launches them with --application-name=Emulator) — an
        UpdateSvc-deployed runtime is the same exe on the same port and does NOT
        count. Returns {state, running, pids, port, port_reachable} (+ hint);
        state=starting means the process is up but the port isn't serving yet —
        wait/re-check before screenshotting; running means safe to screenshot.

        Use this when:
          - deciding whether to run vs stop the emulator (avoid the F5 toggle trap)
          - confirming a preview actually came up before an optix_cdp_screenshot
          - polling after optix_stop_emulator -> optix_run_emulator restart cycles

        Do NOT use this when:
          - you want the UpdateSvc-deployed runtime's port state (optix_runtime_status)
        """
        return core.emulator_status(cfg)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_restart_emulator(project: str | None = None) -> dict:
        """Restart the emulator in one call: stop it if running, start it,
        wait until it's serving — THE way to make a STRUCTURAL edit visible
        (new widget, new binding, layout). Replaces the status/stop/run dance
        and removes the F5-toggle footgun (F5 on a running emulator stops it).
        No save needed — starting stages and saves the current Studio model.

        Use this when:
          - you made a bridge edit and want to SEE it (then optix_cdp_screenshot)
          - the emulator state is unknown and you just want it running fresh

        Do NOT use this when:
          - only exercising already-rendered interactive elements (no restart
            needed — click/type on the live canvas directly)
          - you want it OFF (optix_stop_emulator)
        """
        project = project or core.default_project(cfg)
        if not project:
            return {"launched": False, "error": "no project given and no bridge serving one — pass project or start the bridge"}
        return core.restart_emulator(cfg, project)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_runtime_log_tail(
        lines: int = 100, contains: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Tail the emulator's runtime log (NetLogic output, exceptions) — the
        debug signal when a preview misbehaves.

        Non-blocking by design: one brief shared read of the newest
        FTOptixRuntime.*.log, released immediately (a held handle would block
        the runtime's own writes — never poll this in a tight loop). `lines`
        caps the tail; `contains` filters case-insensitively (e.g.
        contains="error" or a NetLogic class name). NOTE: the log is NOT
        rotated per emulator restart — a contains="error" hit may be HOURS
        old and already fixed; always read the timestamps before treating a
        match as current. Returns {file, lines, returned_lines, truncated}
        or {error: no_log_dir|no_log_file, hint}.

        Use this when:
          - the emulator is up but the canvas is blank/wrong — read the log
            before guessing
          - a NetLogic script should have produced output/thrown
          - optix_emulator_status says starting for suspiciously long

        Do NOT use this when:
          - you want deploy-verb output (this tail is the emulator/runtime log)
          - you're polling for readiness (optix_emulator_status is the probe)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.runtime_log_tail(cfg, project, lines=lines, contains=contains)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_stop_emulator() -> dict:
        """Stop the local FTOptixRuntime emulator (terminates its process).

        An explicit, unambiguous stop — vs F5, which toggles and is easy to
        double-fire. Terminates ONLY emulator instances (--application-name=
        Emulator on the command line); an UpdateSvc-deployed runtime is the same
        exe and is left alone. Returns {stopped, killed_pids, still_running};
        stopped=False with reason=not_running if none was up.

        Use this when:
          - a preview is running and you want it down (not a blind F5 re-press)
          - freeing the runtime port before a fresh run/deploy

        Do NOT use this when:
          - you mean the UpdateSvc-deployed runtime (use optix_runtime_stop)
        """
        return core.stop_emulator(cfg)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_deploy_updatesvc(
        project: str | None = None, run_after: bool = False,
        disable_source_transfer: bool | None = None,
        save_first: bool = True,
    ) -> dict:
        """Deploy a saved project via the FT Optix Application Update Service.

        THE SHIP STEP — deliberate, not the everyday verify. For iteration, use
        optix_run_emulator + optix_cdp_screenshot (much faster, no transfer);
        deploy AFTER the emulator preview confirms the change. It WORKS WITH
        FACTORYTALK OPTIX STUDIO OPEN: the `deploy` verb spawns its
        OWN short-lived Studio to build+transfer the project; your interactive Studio
        AND the design-time bridge stay up the whole time, so you never close
        anything between iterations. (Verified: deploys succeed with Studio open and
        the bridge armed; the bridge survives every deploy.)

        Contrast with optix_deploy, the export path, which REFUSES while Studio is
        open (409 studio_open). So: Studio open + bridge armed -> optix_deploy_updatesvc.

        Mechanism: runs the Studio `deploy` verb, which opens the SAVED project from
        disk, builds it, and transfers it to the UpdateSvc at the configured deploy
        IP (set OPTIX_DEPLOY_IP / OPTIX_DEPLOY_USERNAME / OPTIX_DEPLOY_THUMBPRINT, and
        OPTIX_STUDIO_DEPLOYMENT_PASSWORD in the env). With run_after=True and a
        logged-in deploy user, the verb starts the runtime itself. It SAVES the
        project first by default (save_first) — the deploy reads disk, so unsaved
        bridge/Studio edits would otherwise not ship. Returns {deployed, saved, ip_address, username,
        run_after_deploy, source_transfer_disabled, build_race_warning, returncode,
        stdout_tail}.

        NOTE: when Studio is open the result carries a `build_race_warning`. It is
        ADVISORY ONLY — `deployed` is still true and the change is live. It does NOT
        mean you must close Studio; it just flags that the open Studio's NetSolution
        build could race the verb's build (the verb retries and wins). Do not treat
        it as a refusal or a reason to close Studio.

        disable_source_transfer: skip sending the source .optix tree to the target
        (built runtime only — faster; the default, configurable via
        OPTIX_DEPLOY_KEEP_SOURCE). Pass False to force the source onto the target
        when you'll open/edit the project there.

        Use this when:
          - the emulator preview looks right and you're shipping the change
          - shipping to a real device/UpdateSvc (multi-box, production)
          - you want the verb to deploy AND start the runtime in one call

        Do NOT use this when:
          - you're still iterating/verifying a change (optix_run_emulator is the
            fast default loop — deploy is the ship step)
          - the deploy account/cert aren't configured (run optix_doctor)
        """
        project = project or core.default_project(cfg)
        if not project:
            return {"deployed": False, "error": "no project given and no bridge serving one — pass project or start the bridge"}
        return core.deploy_updatesvc(
            cfg, project, run_after=run_after,
            disable_source_transfer=disable_source_transfer, save_first=save_first)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_add_widget(
        screen: str,
        widgets: list[dict],
        screen_file: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Author an edit that adds widget(s) to a screen — does NOT deploy.

        Generates correct Optix YAML (fresh GUIDs, right indentation, the
        proven binding shape) and returns {edits, file, screen, widgets,
        preview}. Forward `edits` to optix_deploy (combine with other authored
        edits into one deploy). Replaces hand-composing widget YAML — the
        thing that made "add a label" slow and error-prone.

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
          - building the demo's switch+label: one call, two widgets, both
            bound to the same {Model}/<var> (create the var with
            optix_add_model_variable first)

        Do NOT use this when:
          - the widget kind isn't label/switch yet (compose YAML and use an
            anchored optix_deploy edit; tier-2 adds more kinds)
          - the screen has no Children: block (rare; returns a structured
            error pointing you to an anchored edit)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.add_widget(cfg, project, screen, widgets, screen_file=screen_file)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
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
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.add_model_variable(
            cfg, project, name, datatype=datatype, value=value, model_file=model_file
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
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
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.set_property(cfg, project, file, widget, property, value)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_deploy_preflight(project: str | None = None) -> dict:
        """Run every deploy precondition without launching Studio.

        Returns {ready, blockers, warnings, checks}. Each blocker has
        {code, message, hint?}. ready=True iff blockers is empty.

        Checks: project resolves + has .optix manifest, studio_exe present,
        runtime_dir configured, interactive_session=True (Windows DPAPI
        constraint), deploy lock free, git status, runtime port probe
        (informational — a stopped runtime pre-deploy is the normal case),
        and the corruption guard (blocker `studio_open` when FactoryTalk
        Optix Studio is running; blocker `editor_project_open` when VS /
        VS Code has this project open; remediation is closing the app, no
        override exists).

        Use this when:
          - you are about to run optix_deploy on a fresh box and want to
            catch missing config (studio_exe, runtime_dir, interactive
            session) before consuming a Studio launch
          - a prior optix_deploy returned `failed` with no clear cause and
            you want a structured precondition report
          - a read or deploy was refused with `studio_open` and you want
            the structured view of what is blocking

        Do NOT use this when:
          - you already ran a successful deploy in this session (stale
            preflight signal value)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.deploy_preflight(cfg, project)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_studio_version() -> dict:
        """Return FTOptixStudio.exe --version output.

        Use this when:
          - debugging a deploy failure and want to confirm the binary works
          - the user asks which Studio version is installed

        Do NOT use this when:
          - you want full health (use optix_health, which calls this internally)
        """
        return core.studio_version(cfg)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_runtime_start(
        port: int | None = None,
        timeout: float | None = None,
        project: str | None = None,
    ) -> dict:
        """Launch FTOptixRuntime against the swapped runtime tree for `project`.

        Spawns the FTOptixRuntime.exe that Studio's export bundled into
        OPTIX_RUNTIME_DIR/<project>/FTOptixApplication/, detached from the
        service process. Polls the project's runtime port for tcp_reachable
        until `timeout` seconds elapse. The service must run in a Windows
        interactive session (session 1) — same DPAPI constraint as Studio;
        see docs/troubleshooting.md §Studio crashes.

        Args:
          project: project name; runtime tree must already exist under
            cfg.runtime_dir (deploy the project first).
          port: TCP port to probe for liveness (default cfg.runtime_test_port,
            typically 8081). The project's WebPresentationEngine must be
            configured to bind this port.
          timeout: seconds to wait for the port to bind (default 30).

        Returns: {state, project, port, pid, tcp_reachable, started_at,
        confirmed_at, elapsed_seconds, timeout_seconds, runtime_exe}.
        state ∈ {running, not_reachable}.

        Use this when:
          - the user wants to bring up a freshly-deployed project's runtime
            so they (or CDP) can see the rendered HMI
          - after optix_deploy with run_after_deploy=False
          - restarting a runtime to pick up a change (pair with
            optix_runtime_stop first)

        Verify handoff: Optix Web renders the entire HMI into a single
        <canvas> (no DOM targets). For state verification use
        optix_cdp_screenshot / optix_cdp_click — a trusted CDP mouse event
        reaches Optix's hit-tester where synthetic DOM clicks no-op.

        Do NOT use this when:
          - you only want to check if a runtime is already up
            (use optix_runtime_status)
          - the project has not been deployed yet (raises runtime_binary_not_found)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.runtime_start(cfg, project, port=port, timeout=timeout)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_runtime_stop(project: str | None = None) -> dict:
        """Stop FTOptixRuntime processes attached to `project`'s runtime tree.

        WMI-matches FTOptixRuntime.exe processes whose CommandLine references
        the project's runtime tree, then Stop-Process -Force. Idempotent —
        stopping when nothing is running is a successful no-op. Other
        projects' runtimes are not touched.

        Returns: {state, project, runtime_project_dir, stopped_at}.

        Use this when:
          - bouncing a runtime to pick up a code/asset change (call before
            re-deploying or before optix_runtime_start)
          - cleaning up a stale runtime that's holding the port

        Do NOT use this when:
          - you want to stop all FTOptixRuntime processes regardless of project
            (this only kills the ones bound to this project's tree)
        """
        project = _resolve_project(project)
        if not project:
            return _NO_PROJECT
        return core.runtime_stop(cfg, project)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_runtime_status(slot: str) -> dict:
        """Probe a runtime instance's reachability.

        slot: 'test' (default port 8081, OPTIX_RUNTIME_TEST_PORT override)
              | 'mgmt' (default port 8086, OPTIX_HMI_PORT override) for a
                second operator-dashboard runtime, if you run one

        Returns {slot, port, tcp_reachable, checked_at}.

        Use this when:
          - confirming a deploy actually landed (after optix_deploy with
            run_after_deploy=False, the runtime is NOT bounced and the
            caller is responsible for cycling it; this probe confirms)
          - confirming the management runtime is up before pointing the
            user at the HMI URL
          - cold-start drift check after a Windows reboot

        Do NOT use this when:
          - you want to know whether Studio is installed (use optix_health)
          - you want the deploy outcome details (use optix_services_status
            or read /services/last-deploy-tail)
        """
        return core.runtime_status(cfg, slot)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_cdp_click(
        x: float, y: float, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        """Click (x, y) on the Optix runtime canvas via CDP — the RELIABLE path.

        Optix Web renders to a single <canvas> (no DOM targets) and synthetic
        DOM clicks no-op on buttons/switches. This injects a trusted CDP
        Input.dispatchMouseEvent that actually reaches Optix's hit-tester.
        Pass navigate_url to point Chrome at the runtime first (e.g.
        http://localhost:8081/); omit it to click whatever Chrome shows. Pair
        with optix_cdp_screenshot to read coordinates, then click. Returns
        {state, x, y, navigated, clicked_at}.

        Use this when:
          - you need to actually trigger a button/switch on the running HMI
            (state changes), or navigate a NavigationPanel tab
          - a deploy landed but you want to confirm an interaction works

        Do NOT use this when:
          - the chrome-cdp task isn't running (returns cdp_unavailable; run
            optix_doctor / services.ps1 status)
          - you haven't established coordinates from a screenshot first
        """
        return core.cdp_click_runtime(
            cfg, x=x, y=y, navigate_url=navigate_url, settle_seconds=settle_seconds)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_cdp_fill(
        x: float, y: float, text: str,
        submit: str | None = "Enter", select_all: bool = True,
        navigate_url: str | None = None, settle_seconds: float | None = None,
    ) -> dict:
        """Update a field on the running HMI in ONE call: click (x, y), type
        `text`, commit with `submit` (default Enter — values don't stick
        without it).

        THE default way to set a TextBox/SpinBox value — replaces the
        click → type → key trio. select_all (default true) makes the typed
        text REPLACE the current value (a bare click on a TextBox only
        places a caret, so typing would append). submit=None types without
        committing; submit="Tab" for tab-commit fields. Fails loud with
        no_focused_input + a per-step report when the click didn't land on
        an editable field. Returns {state, steps: {clicked, focused_element,
        typed_chars, committed}}.

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
            navigate_url=navigate_url, settle_seconds=settle_seconds)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_cdp_type(
        text: str, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        """Type a string into the focused field on the running Optix HMI (CDP
        Input.insertText).

        The keyboard half of the click-type-Enter pattern for TextBox/SpinBox
        controls: optix_cdp_click the field FIRST (the screenshot shows a
        cursor, or the value select-all-highlighted — that's keyboard-ready),
        then this inserts the whole string at the caret/selection in one CDP
        call. It does NOT click and does NOT commit: **values don't stick
        until Enter** — follow with optix_cdp_key("Enter"). For the common
        set-a-field-value case, prefer optix_cdp_fill (click+type+commit in
        one call); these primitives are for mid-entry screenshots and
        non-standard flows.

        Fails loud with no_focused_input when nothing editable has focus
        (instead of silently no-op'ing). Same trust boundary as
        optix_cdp_click: the one loopback CDP tab this service already owns.
        Returns {state, typed_chars, active_element, navigated, typed_at}.

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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_cdp_key(
        key: str, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        """Press one named key on the running Optix HMI (CDP dispatchKeyEvent,
        keyDown+keyUp).

        THE commit step for field edits: after optix_cdp_click + optix_cdp_type,
        optix_cdp_key("Enter") is what makes the value stick — without it the
        edit is discarded on blur. Keys: Enter, Escape (cancel edit — reverts
        an uncommitted value), Tab (move focus), Backspace, Delete,
        ArrowUp/Down/Left/Right. KNOWN LIMIT: arrow keys do NOT step an Optix
        SpinBox — click its < / > stepper buttons with optix_cdp_click
        instead. Unknown keys return invalid_key + the valid list. Pressing
        with no pending edit is a safe no-op. Returns {state, key, navigated,
        pressed_at}.

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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_cdp_screenshot(
        save_path: str | None = None, quality: int = 65,
        navigate_url: str | None = None, settle_seconds: float | None = None,
        fresh: bool = False, return_image: bool = False,
        region: list[float] | None = None,
    ):
        """Screenshot the running Optix HMI (emulator or deployed runtime) via
        CDP — THE way to visually verify a change.

        IMPORTANT — if your edit is NOT in the screenshot, do NOT conclude it
        failed: a running emulator renders its own loaded snapshot and does not
        pick up Studio edits. optix_restart_emulator, then screenshot again
        before diagnosing. fresh=true forces a page reload before capture —
        use it when a stale frame is suspected (the auto-target otherwise
        skips re-navigation when the tab is already on the runtime).

        *** This is the runtime-verify tool. To confirm a deploy rendered (e.g. "did
        my label show up?"), use THIS — do NOT open the runtime in a general web
        browser (Cowork's native visualize, a Mac/host browser, etc.). *** It drives
        the local chrome-cdp on the SAME box as the runtime (loopback), so it works
        without any external browser and without exposing the runtime on the network.

        You do NOT need to know or pass the runtime URL: called with no navigate_url
        it AUTO-navigates to the local runtime and captures it (skipping the reload if
        the tab is already there, so a click→re-screenshot keeps its state). Pass an
        explicit navigate_url only to point somewhere else; pass navigate_url="" to
        screenshot the current tab as-is.

        CDP Page.captureScreenshot — no tab plumbing. This tool ALWAYS writes the JPEG
        to a file and returns its `path`; by default **read the file back with your
        file tool.** It does NOT put base64 in the JSON: a large b64 string makes some
        hosts try to *render* it inline (Cowork's "visualize"), which can hang for a
        long time on a sandboxed or headless host (verified: file-path runs are fast,
        b64-in-JSON runs stall). **Prefer passing your own `save_path`** in your
        session/output directory so your file tool can definitely read it; omit it and
        the tool picks a temp path.

        `return_image=true` additionally returns the capture as TYPED MCP image
        content (not b64-in-JSON) so the model sees it in the same turn with no file
        round-trip — use when your host's file tool cannot reach the service's
        filesystem. If your host stalls rendering it, go back to the file-path flow.
        Returns {state, path, size_bytes, navigated, captured_at, hint, region}. The
        coordinate system matches optix_cdp_click.

        region: optional [x, y, w, h] to capture just a sub-rectangle instead of the
        full frame (e.g. zooming a vision model in on one widget, or shrinking the
        image before OCR). Coordinate convention: if ALL FOUR values are <= 1.0 they
        are normalized fractions of the viewport (0.5 = mid-screen); if ANY value is
        > 1 the whole list is absolute pixels. A malformed region (wrong length,
        negative, zero width/height, or x/y outside the frame) returns
        state='failed', error='bad_region' rather than raising. The result's
        `region` field echoes back the resolved absolute-pixel [x, y, w, h] (or null
        when region wasn't passed) — use it to sanity-check what actually got
        captured.

        Use this when:
          - VALIDATING a deploy: capture the runtime HMI to confirm the change is live
          - capturing the HMI to locate a widget before optix_cdp_click
          - zooming into one widget/region instead of the whole canvas (region)

        Do NOT use this when:
          - the chrome-cdp task isn't running (returns cdp_unavailable; run
            optix_doctor / services.ps1 status)
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
            fresh=fresh, region=region)
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_cdp_ocr(
        navigate_url: str | None = None, settle_seconds: float | None = None,
        psm: int = 6,
    ) -> dict:
        """OCR the runtime canvas via tesseract — an OPT-IN, text-only read-back.

        Prefer optix_cdp_screenshot + a vision model for verify: it reads color,
        layout, and text. Use THIS only when vision isn't available on the caller
        (a headless/cron run) or as a fallback signal when a capture renders blank.
        It captures the runtime through the same path as optix_cdp_screenshot, then
        runs `tesseract` on the JPEG and returns the recognized text. Returns
        {state, text, size_bytes, navigated, captured_at}; if tesseract isn't
        installed it returns state='failed', error='tesseract_not_installed' with an
        install hint (optional infrastructure — it never crashes the loop).

        Use this when:
          - a headless caller has no vision model but needs to read back rendered text
          - a screenshot came back blank and you want any text signal at all

        Do NOT use this when:
          - a vision model is available (use optix_cdp_screenshot — it sees more)
          - you need to verify color/position (OCR is text-only)
        """
        return core.cdp_ocr_runtime(
            cfg, navigate_url=navigate_url, settle_seconds=settle_seconds, psm=psm)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_cdp_read_text(
        region: list[float] | None = None, navigate_url: str | None = None,
        settle_seconds: float | None = None, psm: int = 6,
    ) -> dict:
        """OCR a region of the runtime canvas via tesseract — THE cheap check for
        "does the screen/widget say X" — zero vision tokens.

        Captures through the same region-clip path as optix_cdp_screenshot (see its
        docstring for the region coordinate convention: values all <= 1.0 are
        normalized viewport fractions, any value > 1 means absolute pixels), then
        runs tesseract on the JPEG. Omit `region` to OCR the full frame. Returns
        {state, text, region, size_bytes, navigated, captured_at}. If tesseract
        isn't installed, returns state='failed', error='tesseract_not_installed'
        with an install hint rather than raising — optional infrastructure, same
        contract as optix_cdp_ocr. A malformed region degrades the same way
        (error='bad_region').

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
            settle_seconds=settle_seconds, psm=psm)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_cdp_find_text(
        text: str, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        """Locate `text` on the runtime canvas via tesseract word boxes — to find
        a labeled control to click, or to build a navigation route.

        Full-frame capture (no region — you're locating something, so you don't
        know its coordinates yet). Matching is case-insensitive; a multi-word
        `text` query only matches ADJACENT words on the same tesseract line (words
        on different lines never join). Words with OCR confidence < 40 are dropped
        before matching. Returns {state, found, matches: [{text, confidence,
        bbox_px: [x,y,w,h], bbox_norm: [x,y,w,h], center_px: [x,y]}], viewport: {w,
        h}}. No match is NOT an error — found=false, matches=[]. Requires
        tesseract: missing binary returns state='failed',
        error='tesseract_not_installed' (same degradation contract as
        optix_cdp_ocr / optix_cdp_read_text), never raises.

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
            cfg, text, navigate_url=navigate_url, settle_seconds=settle_seconds)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def optix_cdp_navigate(
        route: str, routes_path: str, expect: bool = True,
        navigate_url: str | None = None,
    ) -> dict:
        """Zero-screenshot navigation to a banked screen: replays a sequence of
        clicks from a routes JSON file instead of screenshot -> locate ->
        click, click again.

        Routes file format (version 1): `{"version": 1, "routes": {"<name>":
        {"steps": [{"click": [x, y], "settle_seconds": 0.5, "expect_text":
        "Setup Values"}]}}}`. `click` uses the SAME coordinate convention as
        optix_cdp_screenshot's `region`: both values <= 1.0 are normalized
        viewport fractions, any value > 1 is absolute pixels — portable
        across window sizes. Convention: bank routes at `dev/ftx_ui_map.json`
        in the project workspace — see the optix-blind-authoring skill for
        the cache workflow (discover once with optix_cdp_find_text, bank the
        route, navigate blind from then on).

        expect_text verification needs tesseract: with expect=true (the
        default) and a step carrying expect_text, this OCRs the frame after
        the click and checks expect_text is a case-insensitive substring of
        the recognized text. The FIRST failed expectation stops the route
        immediately — later steps do not run — and returns
        error='expectation_failed' with the step index and a read_back
        excerpt: fail loud rather than drift onto the wrong screen. If
        tesseract isn't installed, the checks are skipped (not a failure) and
        the response carries ocr_unavailable=true — the clicks still ran.

        Returns {state, route, steps_run, verified_steps, ocr_unavailable?,
        navigated, finished_at}. Never raises for a bad routes file: missing
        path -> error='routes_file_not_found'; bad JSON ->
        'routes_file_invalid'; unknown route -> 'route_not_found' (with
        `available` listing known routes); a malformed step -> 'route_invalid'
        naming the `step` index.

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
            navigate_url=navigate_url)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def optix_cdp_restart(allow_restart: bool = True) -> dict:
        """Recover the chrome-cdp instance that screenshot/click drive.

        Usually you do NOT need this — screenshot/click self-heal once on their
        own (open a page if Chrome is up but tab-less, or restart the
        ftx-mcp-chrome-cdp task if Chrome is down). Call this to force
        that recovery explicitly, or to check/repair after a reboot. Set
        allow_restart=False to only open a page (never relaunch the process).
        Returns {state, alive, has_page, restarted, detail} — state is
        ok|opened_page|restarted|failed.

        Use this when:
          - a verify tool reported cdp_unavailable and you want to repair it
          - after a reboot, to bring canvas-verify back without a full restart

        Do NOT use this when:
          - things are working — the tools already self-heal; this is a manual
            override, not a routine step
        """
        return core.ensure_chrome_cdp(cfg, allow_restart=allow_restart)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def optix_services_status() -> dict:
        """Aggregate health + studio version + runtime/cdp probes.

        The HMI status-tile payload: health, studio version, the runtime
        test-port probe, and a CDP-endpoint probe (the chrome-cdp task used
        for canvas verify).

        Use this when:
          - rendering an operator dashboard's services panel
          - the user asks "what's the state of the deploy stack right now?"

        Do NOT use this when:
          - you only need health (use optix_health)
          - you want the last-deploy outcome details (read
            /services/last-deploy-tail directly; not surfaced as an MCP tool)
        """
        return core.services_status(cfg)

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
                        "(deploy superset of read superset of health)"
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
