"""FastAPI thin wrapper over service.core (see docs/architecture.md)."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import __version__, core
from .deploy_lock import LockHeld


class Edit(BaseModel):
    """One edit. Exactly one mode per edit (docs/architecture.md, Edit modes):
    content -> full replace; find+replace -> anchored replace;
    insert_after_anchor+block -> anchored insert. Mode validation lives in
    core.py so the MCP and HTTP surfaces behave identically."""

    path: str = Field(..., description="Relative path under the project dir")
    content: str | None = Field(None, description="Full file content (UTF-8, no BOM)")
    find: str | None = Field(None, description="Literal text to replace (newlines auto-match file EOL)")
    replace: str | None = Field(None, description="Replacement for `find` (may be empty)")
    expect_count: int | None = Field(None, description="Required occurrence count for `find` (default 1)")
    insert_after_anchor: str | None = Field(None, description="Literal anchor; block inserted after its line")
    block: str | None = Field(None, description="Lines to insert after the anchor line")

    def to_core(self) -> dict:
        """Drop unset fields so core.py sees only the chosen mode's keys."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


class DeployRequest(BaseModel):
    edits: list[Edit] = Field(default_factory=list)
    commit_message: str = "Automated edit"
    run_after_deploy: bool = True


class AddWidgetRequest(BaseModel):
    screen: str
    widgets: list[dict] = Field(..., description="[{kind:'label'|'switch', name, ...}]")
    screen_file: str | None = None


class AddModelVarRequest(BaseModel):
    name: str
    datatype: str = "Boolean"
    value: bool = False
    model_file: str = "Nodes/Model/Model.yaml"


class SetPropertyRequest(BaseModel):
    file: str
    widget: str
    property: str
    value: str


class BridgeWidgetRequest(BaseModel):
    screen: str
    name: str
    widget_type: str = "Label"


class BridgeSetPropertyRequest(BaseModel):
    node_path: str
    name: str
    value: str
    locale: str = "en-US"


class BridgeBindRequest(BaseModel):
    node_path: str
    name: str
    source_path: str | None = None
    mode: str = "Read"
    raw_path: str | None = None   # literal NodePath (alias/template late binding)


def make_app(cfg: core.Config) -> FastAPI:
    app = FastAPI(
        title="ftx-mcp HTTP",
        version=__version__,
        description="FastAPI surface for FactoryTalk Optix authoring and deploys.",
    )

    @app.exception_handler(core.CoreError)
    async def _core_handler(_: Request, exc: core.CoreError) -> JSONResponse:
        body: dict = {
            "code": exc.code,
            "message": str(exc) or exc.code,
        }
        if exc.hint:
            body["hint"] = exc.hint
        if exc.docs_anchor:
            body["docs_url"] = f"docs/troubleshooting.md#{exc.docs_anchor}"
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(LockHeld)
    async def _lock_handler(_: Request, exc: LockHeld) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "code": "deploy_lock_held",
                "message": "another deploy is in flight",
                "hint": "wait for the in-flight deploy to finish, or check the lock holder PID",
                "lock": exc.lock_state,
            },
        )

    @app.get("/health")
    def health() -> dict:
        return core.health(cfg)

    @app.get("/skills")
    def list_skills_endpoint() -> dict:
        return core.list_skills(cfg)

    @app.get("/skills/{name}")
    def get_skill_endpoint(name: str) -> dict:
        return core.get_skill(cfg, name)

    @app.get("/projects")
    def list_projects() -> dict:
        return {"projects": core.list_projects(cfg)}

    @app.get("/projects/{project}/files/{path:path}")
    def read_file(
        project: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict:
        return core.read_file(
            cfg, project, path, start_line=start_line, end_line=end_line
        )

    @app.get("/projects/{project}/find")
    def find_endpoint(
        project: str,
        query: str,
        glob: str = "**/*",
        max_results: int = 200,
        context_lines: int = 2,
        case_sensitive: bool = False,
    ) -> dict:
        return core.find_in_project(
            cfg,
            project,
            query,
            glob=glob,
            max_results=max_results,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )

    @app.get("/projects/{project}/map")
    def project_map_endpoint(
        project: str, path: str | None = None, depth: int | None = None,
        max_nodes: int = 800, ids: bool = False, match: str | None = None,
        format: str = "outline",
    ) -> dict:
        return core.get_project_map(
            cfg, project, path=path, depth=depth, max_nodes=max_nodes,
            ids=ids, match=match, fmt=format)

    @app.get("/projects/{project}/screens")
    def list_screens_endpoint(project: str) -> dict:
        return core.list_screens(cfg, project)

    @app.post("/projects/{project}/widgets")
    def add_widget_endpoint(project: str, req: AddWidgetRequest) -> dict:
        return core.add_widget(
            cfg, project, req.screen, req.widgets, screen_file=req.screen_file
        )

    @app.post("/projects/{project}/model-variables")
    def add_model_var_endpoint(project: str, req: AddModelVarRequest) -> dict:
        return core.add_model_variable(
            cfg, project, req.name, datatype=req.datatype, value=req.value,
            model_file=req.model_file,
        )

    @app.post("/projects/{project}/set-property")
    def set_property_endpoint(project: str, req: SetPropertyRequest) -> dict:
        return core.set_property(
            cfg, project, req.file, req.widget, req.property, req.value
        )

    @app.post("/projects/{project}/deploy")
    def deploy_endpoint(project: str, req: DeployRequest) -> dict:
        dr = core.DeployRequest(
            edits=[e.to_core() for e in req.edits],
            commit_message=req.commit_message,
            run_after_deploy=req.run_after_deploy,
        )
        return core.deploy(cfg, project, dr)

    @app.post("/projects/{project}/deploy/preflight")
    def deploy_preflight_endpoint(project: str) -> dict:
        return core.deploy_preflight(cfg, project)

    @app.get("/runtime/{slot}/status")
    def runtime_status(slot: str) -> dict:
        return core.runtime_status(cfg, slot)

    @app.post("/projects/{project}/runtime/start")
    def runtime_start_endpoint(
        project: str,
        port: int | None = None,
        timeout: float | None = None,
    ) -> dict:
        return core.runtime_start(cfg, project, port=port, timeout=timeout)

    @app.post("/projects/{project}/runtime/stop")
    def runtime_stop_endpoint(project: str) -> dict:
        return core.runtime_stop(cfg, project)

    @app.get("/services/status")
    def services_status() -> dict:
        return core.services_status(cfg)

    @app.get("/projects/{project}/git/log")
    def project_git_log(project: str, limit: int = 10) -> dict:
        return {"commits": core.git_log(cfg, project, limit=limit)}

    @app.get("/services/last-deploy-tail")
    def last_deploy_tail(project: str | None = None) -> dict:
        # Optional ?project=<name> filter — when set, returns the most
        # recent buffer entry whose `project` field matches. Falls back
        # to the global last entry when omitted. Lets the HMI's
        # SelectedProject drive what tail is shown.
        entry = core.last_deploy_tail(cfg, project=project)
        return {"deploy": entry}

    # --- v1.0 capabilities: doctor / save / UpdateSvc deploy / serve / bridge ---

    @app.get("/doctor")
    def doctor_endpoint() -> dict:
        return core.doctor(cfg)

    # Tool catalog for the console — built from the live MCP registry so the
    # UI can never drift from the real tool surface (names, one-line summary
    # for hover, read/write/destructive kind). Built once per process.
    _tool_catalog: list[dict] = []

    def _tools_catalog() -> list[dict]:
        if not _tool_catalog:
            from .mcp_app import make_mcp
            for t in make_mcp(cfg)._tool_manager.list_tools():
                ann = t.annotations
                kind = ("read" if ann and ann.readOnlyHint
                        else "destructive" if ann and ann.destructiveHint
                        else "write")
                summary = (t.description or "").strip().splitlines()[0] if t.description else ""
                _tool_catalog.append({"name": t.name, "kind": kind, "summary": summary})
            _tool_catalog.sort(key=lambda x: (x["kind"], x["name"]))
        return _tool_catalog

    @app.get("/ui/stats")
    def ui_stats_endpoint() -> dict:
        out = core.ui_stats(cfg)
        out.setdefault("capabilities", {})["tools"] = _tools_catalog()
        return out

    @app.get("/ui", response_class=HTMLResponse)
    def ui_dashboard() -> str:
        import pathlib
        p = pathlib.Path(__file__).parent / "static" / "dashboard.html"
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return "<h1>ftx-mcp console</h1><p>dashboard.html not found</p>"

    @app.post("/projects/{project}/save")
    def save_endpoint(project: str) -> dict:
        return core.save(cfg, project)

    @app.post("/projects/{project}/run/emulator")
    def run_emulator_endpoint(project: str, save_first: bool = False) -> dict:
        return core.run_emulator(cfg, project, save_first=save_first)

    @app.get("/emulator/status")
    def emulator_status_endpoint() -> dict:
        return core.emulator_status(cfg)

    @app.post("/projects/{project}/bridge/add-bound-widget")
    def bridge_add_bound_widget_endpoint(
        project: str, screen: str, name: str, widget_type: str,
        left: float | None = None, top: float | None = None,
        width: float | None = None, height: float | None = None,
        text: str | None = None, bind_property: str | None = None,
        source_path: str | None = None, mode: str = "Read",
    ) -> dict:
        return core.bridge_add_bound_widget(
            cfg, project, screen, name, widget_type, left=left, top=top,
            width=width, height=height, text=text,
            bind_property=bind_property, source_path=source_path, mode=mode)

    @app.post("/projects/{project}/bridge/add-navigation-panel-item")
    def bridge_add_nav_item_endpoint(
        project: str, panel_path: str, title: str,
        screen_path: str | None = None, name: str | None = None,
    ) -> dict:
        return core.bridge_add_navigation_panel_item(
            cfg, project, panel_path, title, screen_path=screen_path, name=name)

    @app.post("/projects/{project}/bridge/create-folder")
    def bridge_create_folder_endpoint(project: str, parent: str, name: str) -> dict:
        return core.bridge_create_folder(cfg, project, parent, name)

    @app.post("/projects/{project}/bridge/create-object")
    def bridge_create_object_endpoint(
        project: str, parent: str, name: str, object_type: str | None = None,
    ) -> dict:
        return core.bridge_create_object(cfg, project, parent, name, object_type)

    @app.post("/projects/{project}/bridge/create-type")
    def bridge_create_type_endpoint(
        project: str, name: str, parent: str, base_type: str | None = None,
    ) -> dict:
        return core.bridge_create_type(cfg, project, name, parent, base_type)

    @app.post("/projects/{project}/bridge/move-node")
    def bridge_move_node_endpoint(
        project: str, node_path: str, new_parent: str,
        new_name: str | None = None,
    ) -> dict:
        return core.bridge_move_node(cfg, project, node_path, new_parent, new_name)

    @app.post("/projects/{project}/bridge/convert-to-type")
    def bridge_convert_to_type_endpoint(
        project: str, node_path: str, type_name: str,
        types_folder: str = "UI/Templates", replace: bool = True,
    ) -> dict:
        return core.bridge_convert_to_type(
            cfg, project, node_path, type_name, types_folder, replace)

    @app.post("/projects/{project}/emulator/restart")
    def restart_emulator_endpoint(project: str) -> dict:
        return core.restart_emulator(cfg, project)

    @app.post("/emulator/stop")
    def stop_emulator_endpoint() -> dict:
        return core.stop_emulator(cfg)

    @app.get("/projects/{project}/runtime/log-tail")
    def runtime_log_tail_endpoint(
        project: str, lines: int = 100, contains: str | None = None,
    ) -> dict:
        return core.runtime_log_tail(cfg, project, lines=lines, contains=contains)

    @app.post("/cdp/ocr")
    def cdp_ocr_endpoint(
        navigate_url: str | None = None, settle_seconds: float | None = None,
        psm: int = 6,
    ) -> dict:
        return core.cdp_ocr_runtime(
            cfg, navigate_url=navigate_url, settle_seconds=settle_seconds, psm=psm)

    @app.post("/projects/{project}/deploy/updatesvc")
    def deploy_updatesvc_endpoint(
        project: str, run_after: bool = False,
        disable_source_transfer: bool | None = None,
    ) -> dict:
        return core.deploy_updatesvc(
            cfg, project, run_after=run_after,
            disable_source_transfer=disable_source_transfer)

    @app.post("/projects/{project}/bridge/widget")
    def bridge_widget_endpoint(project: str, req: BridgeWidgetRequest) -> dict:
        return core.bridge_create_widget(cfg, project, req.screen, req.name, req.widget_type)

    @app.post("/projects/{project}/bridge/set-property")
    def bridge_set_property_endpoint(project: str, req: BridgeSetPropertyRequest) -> dict:
        return core.bridge_set_property(cfg, project, req.node_path, req.name, req.value, req.locale)

    @app.post("/projects/{project}/bridge/bind")
    def bridge_bind_endpoint(project: str, req: BridgeBindRequest) -> dict:
        return core.bridge_bind_property(
            cfg, project, req.node_path, req.name, req.source_path, req.mode,
            req.raw_path)

    @app.post("/projects/{project}/bridge/ensure-web-engine")
    def bridge_ensure_web_engine_endpoint(
        project: str, port: int = 8081, ip: str = "0.0.0.0",
    ) -> dict:
        return core.bridge_ensure_web_engine(cfg, project, port=port, ip=ip)

    @app.post("/runtime/cdp-click")
    def cdp_click_endpoint(
        x: float, y: float, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        return core.cdp_click_runtime(
            cfg, x=x, y=y, navigate_url=navigate_url, settle_seconds=settle_seconds)

    @app.post("/runtime/cdp-fill")
    def cdp_fill_endpoint(
        x: float, y: float, text: str,
        submit: str | None = "Enter", select_all: bool = True,
        navigate_url: str | None = None, settle_seconds: float | None = None,
    ) -> dict:
        return core.cdp_fill_runtime(
            cfg, x=x, y=y, text=text, submit=submit, select_all=select_all,
            navigate_url=navigate_url, settle_seconds=settle_seconds)

    @app.post("/runtime/cdp-type")
    def cdp_type_endpoint(
        text: str, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        return core.cdp_type_runtime(
            cfg, text=text, navigate_url=navigate_url, settle_seconds=settle_seconds)

    @app.post("/runtime/cdp-key")
    def cdp_key_endpoint(
        key: str, navigate_url: str | None = None,
        settle_seconds: float | None = None,
    ) -> dict:
        return core.cdp_key_runtime(
            cfg, key=key, navigate_url=navigate_url, settle_seconds=settle_seconds)

    @app.post("/runtime/cdp-screenshot")
    def cdp_screenshot_endpoint(
        save_path: str | None = None, quality: int = 65,
        navigate_url: str | None = None, settle_seconds: float | None = None, fresh: bool = False,
    ) -> dict:
        return core.cdp_screenshot_runtime(
            cfg, save_path=save_path, quality=quality,
            navigate_url=navigate_url, settle_seconds=settle_seconds, fresh=fresh)

    @app.post("/runtime/cdp-restart")
    def cdp_restart_endpoint(allow_restart: bool = True) -> dict:
        return core.ensure_chrome_cdp(cfg, allow_restart=allow_restart)

    if not cfg.enable_deploy:
        # Deploy integration is statically disabled in this distribution — drop the
        # deploy/runtime routes and the file-edit authoring that feeds them.
        _deploy_paths = {
            "/projects/{project}/deploy", "/projects/{project}/deploy/preflight",
            # updatesvc was MISSING from this set — the hardware-shipping route
            # was reachable with deploy off (found 2026-07-17 hardening pass).
            "/projects/{project}/deploy/updatesvc",
            "/services/last-deploy-tail",
            "/projects/{project}/widgets", "/projects/{project}/model-variables",
            "/projects/{project}/set-property", "/runtime/{slot}/status",
            "/projects/{project}/runtime/start", "/projects/{project}/runtime/stop",
        }
        app.router.routes = [r for r in app.router.routes
                             if getattr(r, "path", None) not in _deploy_paths]
    return app
