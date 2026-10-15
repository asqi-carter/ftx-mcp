"""Process entrypoint: serves FastAPI HTTP on :8765 and FastMCP streamable-HTTP on :8766 (path ``MCP_PATH``).

Both surfaces share `core.Config` (one source of truth) and run in the same
asyncio event loop via uvicorn.

Auth:
- `FTX_AUTH_REQUIRED` gate. The
  default is `false`: the common install is loopback-only, where a
  bearer token adds ~no security but real friction. Set
  `FTX_AUTH_REQUIRED=true` to require tokens (mandatory for a LAN bind).
- LAN-bind refusal matrix — exits 3 on disallowed bind/auth combinations
  (`OPTIX_BIND_HOST != 127.0.0.1` with `FTX_AUTH_REQUIRED=false`, or
  with auth required but zero tokens).
- `service.auth.AuthMiddleware` mounted around both ASGI surfaces. Same
  middleware instance, same scope rules, same token table.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading

import traceback
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn

from . import __version__, _cdp, _client_registry, core
from .auth import AuthMiddleware, TokenStore
from .http_app import make_app
from .mcp_app import make_mcp


# URL path the FastMCP streamable-HTTP app is served on (also declared in server.json).
MCP_PATH = "/mcp"


def _port_holder(host: str, port: int) -> str | None:
    """Returns a description if the port is already bound, None if free."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.bind((host, port))
        return None
    except OSError as e:
        return f"{e.__class__.__name__}: {e}"
    finally:
        s.close()


def _port_is_served(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """True if something is already ACCEPTING connections on host:port.

    Distinct from `_port_holder` (a bind probe): connect_ex returning 0 means
    a live listener answered, i.e. another instance is already serving — the
    signature of the observed venv-vs-system-python double-launch, where two
    interpreters start near-simultaneously, both clear the early bind probe
    while the port is momentarily free, and then one wins the bind. connect_ex
    never raises for the ordinary refused/timeout cases; it returns a non-zero
    errno, which we read as "free"."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        # Unresolvable host / socket-layer failure: treat as "not served" so a
        # probe glitch never blocks a legitimate boot (the bind will surface any
        # real problem authoritatively).
        return False
    finally:
        s.close()


def guard_double_launch(cfg: core.Config) -> int:
    """Late (pre-serve) guard against a racing second instance.

    The early bind probe in `main()` catches a port that is ALREADY taken when
    the process starts. This guard closes the TOCTOU window between that probe
    and the actual uvicorn bind: run it right before serving and, if the MCP or
    HTTP port has since started answering, exit non-zero cleanly instead of
    letting uvicorn crash with a raw bind traceback.

    Returns 0 to proceed, or a non-zero exit code when a live listener is
    detected. Emits one clear line per served port on stderr."""
    for label, port in (
        ("MCP", cfg.bind_mcp_port),
        ("HTTP", cfg.bind_http_port),
    ):
        if _port_is_served(cfg.bind_host, port):
            print(
                f"FAIL: {label} port {port} already served by another instance "
                "— exiting to avoid a double-launch.",
                file=sys.stderr,
                flush=True,
            )
            print(
                "  A second ftx-mcp (often a venv-vs-system-python race) is "
                "already listening. This process is exiting; the running one "
                "keeps serving.",
                file=sys.stderr,
                flush=True,
            )
            return 4
    return 0


def check_lan_bind_safety(
    cfg: core.Config, store: TokenStore
) -> tuple[int, list[str], list[str]]:
    """Implement the bind/auth refusal matrix (docs/security.md).

    Returns `(exit_code, fail_messages, warn_messages)`:
      - exit_code == 0 means proceed; > 0 means abort with that code.
      - fail_messages are emitted on stderr before the abort.
      - warn_messages are emitted on stdout when the service starts.

    The matrix:
      | bind                 | auth_required | tokens | outcome |
      |----------------------|---------------|--------|---------|
      | 127.0.0.1            | false         | n/a    | start   |
      | 127.0.0.1            | true          | ≥ 1    | start   |
      | 127.0.0.1            | true          | 0      | start (WARN — no tokens) |
      | non-loopback         | false         | n/a    | exit 3  |
      | non-loopback         | true          | 0      | exit 3  |
      | non-loopback         | true          | ≥ 1    | start (WARN — LAN bind) |
    """
    is_loopback = cfg.bind_host == "127.0.0.1"
    auth_on = cfg.auth_required
    n_tokens = len(store)

    fails: list[str] = []
    warns: list[str] = []

    if not is_loopback and not auth_on:
        fails.append(
            f"FAIL: OPTIX_BIND_HOST={cfg.bind_host} (LAN bind) with "
            "FTX_AUTH_REQUIRED=false. The loopback-no-auth opt-out is only "
            "valid when bind is 127.0.0.1. LAN binding without auth is refused."
        )
        fails.append(
            "  Either set FTX_AUTH_REQUIRED=true and run "
            "bootstrap/issue-token.ps1, or revert to OPTIX_BIND_HOST=127.0.0.1."
        )
        return 3, fails, warns

    if not is_loopback and auth_on and n_tokens == 0:
        fails.append(
            f"FAIL: OPTIX_BIND_HOST={cfg.bind_host} with FTX_AUTH_REQUIRED=true "
            "but no tokens issued. Nothing can authenticate."
        )
        fails.append(
            "  Run bootstrap/issue-token.ps1 to issue at least one token, then restart."
        )
        return 3, fails, warns

    if is_loopback and auth_on and n_tokens == 0:
        warns.append(
            "WARN: FTX_AUTH_REQUIRED=true but no tokens have been issued. "
            "All requests will 401 until bootstrap/issue-token.ps1 issues one."
        )

    if not is_loopback and auth_on and n_tokens >= 1:
        warns.append(
            f"WARN: binding to {cfg.bind_host} (LAN bind) with {n_tokens} token(s) "
            "issued. Restrict reachability via firewall or Tailscale ACLs — "
            "auth is necessary but not sufficient against opportunistic LAN scans."
        )

    return 0, fails, warns


def _install_client_capture(mcp: Any) -> None:
    """Record the connected MCP client's name+version for the /ui dashboard.

    The MCP `initialize` handshake carries `clientInfo`, but the low-level
    server handles `initialize` entirely inside `ServerSession` — there is no
    registerable initialize callback. By the time ANY post-init request reaches
    the server's `_handle_request`, though, `session.client_params.clientInfo`
    is populated (every client sends `tools/list` immediately after
    `initialize`). Wrapping `_handle_request` here is a main.py-level seam that
    leaves mcp_app.py untouched. It records the identity into the process-global
    `_client_registry`; the /ui surface reads it.

    Fully guarded: a shape change in the MCP SDK degrades to "no capture", never
    to a crash of request dispatch. The wrap is a no-op if the private handler
    is absent (SDK shape guard)."""
    server = getattr(mcp, "_mcp_server", None)
    orig = getattr(server, "_handle_request", None)
    if orig is None:
        return  # pragma: no cover - SDK shape guard

    async def _handle_request_capturing(message, req, session, lifespan_context, raise_exceptions):  # type: ignore[no-untyped-def]
        try:
            params = getattr(session, "client_params", None)
            info = getattr(params, "clientInfo", None) if params else None
            if info is not None:
                _client_registry.record_client(
                    getattr(info, "name", None), getattr(info, "version", None)
                )
        except Exception:  # pragma: no cover - never break dispatch over telemetry
            pass
        return await orig(message, req, session, lifespan_context, raise_exceptions)

    server._handle_request = _handle_request_capturing


def _wrap_with_auth(
    app: Callable[..., Awaitable[None]],
    store: TokenStore,
    *,
    auth_required: bool,
) -> Callable[..., Awaitable[None]]:
    """Wrap an ASGI app in `AuthMiddleware`. Identity-shaped helper so
    tests can verify the wrapping happens without spinning up uvicorn."""
    return AuthMiddleware(app, store, auth_required=auth_required)


def build_token_store(cfg: core.Config) -> TokenStore:
    """Construct a TokenStore from cfg, returning an empty store if the
    file is missing — keeps the Phase 1 default loopback path running
    without ceremony when no tokens have been issued."""
    return TokenStore(cfg.tokens_path)


def _ensure_state_dirs(cfg: core.Config) -> None:
    """Self-create state_dir AND runtime_dir at startup.

    The service owns creation of its state dirs — the installer cannot be
    trusted to have made them: setup.ps1 run from an MSIX-packaged shell
    (Store-build Claude Desktop, i.e. the Cowork quick-install path) gets
    its %LOCALAPPDATA% writes virtualized into the app's package overlay,
    which the scheduled task launching this service never sees.
    """
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if cfg.runtime_dir:
        try:
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # OPTIX_RUNTIME_DIR may point at a volume that is legitimately
            # offline; that must stay a red /health flag, not a startup crash.
            pass


def _should_hide_console(argv: list[str]) -> bool:
    """Either mechanism: --hide-console for a one-off, OPTIX_HIDE_CONSOLE for
    an operator who wants it on every start without re-registering the task.
    Same truthy set as FTX_AUTH_REQUIRED so there is one convention to learn.

    Split out from the Win32 call so the DECISION is testable off-Windows.
    """
    if "--hide-console" in argv:
        return True
    return os.environ.get("OPTIX_HIDE_CONSOLE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _hide_console_if_asked(argv: list[str]) -> None:
    """Hide this process's console window on request.

    Opt in with --hide-console or OPTIX_HIDE_CONSOLE=1.

    Windows decides console attachment from the executable's SUBSYSTEM, not
    from any runtime flag: python.exe is console-subsystem so Windows always
    allocates one. The alternative -- registering the task against
    pythonw.exe -- costs two things this does not: pythonw has no
    stdout/stderr at all (they are None, so the first log line raises), and
    it leaves no console for child processes to inherit, so every shell-out
    allocates a fresh one and flashes on screen.

    Hiding the console we already own keeps real stdout/stderr and keeps a
    console for children to inherit (they draw into a hidden window, i.e.
    nothing). One launcher, one code path. The only cost is a brief flicker
    at startup before the window is hidden -- once per service start, not
    once per shell-out.
    """
    if os.name != "nt" or not _should_hide_console(argv):
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        # Never let a cosmetic window tweak stop the service from starting.
        pass


def main(argv: list[str] | None = None) -> int:
    _hide_console_if_asked(list(argv or []))
    cfg = core.Config.from_env()
    _ensure_state_dirs(cfg)

    conflicts = []
    for label, port in (
        ("HTTP", cfg.bind_http_port),
        ("MCP", cfg.bind_mcp_port),
    ):
        msg = _port_holder(cfg.bind_host, port)
        if msg is not None:
            conflicts.append((label, port, msg))
    if conflicts:
        print(
            f"FAIL: cannot bind ftx-mcp on {cfg.bind_host} - port already in use:",
            file=sys.stderr,
            flush=True,
        )
        for label, port, msg in conflicts:
            print(f"  {label} :{port} -> {msg}", file=sys.stderr, flush=True)
        print(
            "  Override with OPTIX_HTTP_PORT / OPTIX_MCP_PORT, or kill the holder and retry. "
            "See docs/troubleshooting.md.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    store = build_token_store(cfg)
    exit_code, fails, warns = check_lan_bind_safety(cfg, store)
    if exit_code != 0:
        for line in fails:
            print(line, file=sys.stderr, flush=True)
        return exit_code

    # Warm psutil's process map off the request path: the FIRST process_iter
    # can take tens of seconds (per-process handle opens, slower still when
    # endpoint-protection hooks them), every later scan ~0.1s. Without this,
    # the first emulator status/stop/restart after service boot eats that
    # cold hit.
    threading.Thread(target=core._emulator_pids, daemon=True).start()

    http_app: Any = make_app(cfg)
    mcp = make_mcp(cfg)
    _install_client_capture(mcp)
    mcp_asgi: Any = mcp.streamable_http_app()

    http_app_authed = _wrap_with_auth(http_app, store, auth_required=cfg.auth_required)
    mcp_asgi_authed = _wrap_with_auth(mcp_asgi, store, auth_required=cfg.auth_required)

    http_server = uvicorn.Server(uvicorn.Config(
        http_app_authed, host=cfg.bind_host, port=cfg.bind_http_port,
        log_level="info", access_log=False,
    ))
    mcp_server = uvicorn.Server(uvicorn.Config(
        mcp_asgi_authed, host=cfg.bind_host, port=cfg.bind_mcp_port,
        log_level="info", access_log=False,
    ))

    print(f"ftx-mcp v{__version__}", flush=True)
    print(f"  HTTP  http://{cfg.bind_host}:{cfg.bind_http_port}", flush=True)
    print(f"  MCP   http://{cfg.bind_host}:{cfg.bind_mcp_port}{MCP_PATH} (streamable-HTTP)", flush=True)
    print(f"  UI    http://{cfg.bind_host}:{cfg.bind_http_port}/ui", flush=True)
    print(f"  state {cfg.state_dir}", flush=True)
    if cfg.auth_required:
        print(f"  auth  required (tokens loaded: {len(store)})", flush=True)
    else:
        print("  auth  disabled (loopback only)", flush=True)
    # CDP status at boot: field reports show the chrome-cdp task drifting out
    # of sync with the service (orphaned chrome after reinstall, task never
    # started). One probe line here answers "is verify going to work?" without
    # a support round-trip. probe() never raises; 1s cap keeps boot snappy.
    _cdp_state = _cdp.probe(cfg.cdp_url, timeout=1.0)
    if _cdp_state["alive"]:
        page = "drivable page" if _cdp_state["has_page"] else "no page target"
        print(f"  cdp   ok ({cfg.cdp_url}, {page})", flush=True)
    else:
        print(
            f"  cdp   not running ({cfg.cdp_url}) - canvas verify unavailable; "
            "start: bootstrap\\services.ps1 start",
            flush=True,
        )
    for line in warns:
        print(f"  {line}", flush=True)

    interactive = core._is_interactive_session()
    if interactive is False:
        print(
            "  WARNING: not running in an interactive logon session. "
            "Studio deploys will crash with 0xC0000005 because DPAPI keys are "
            "bound to interactive sessions. See docs/troubleshooting.md.",
            file=sys.stderr,
            flush=True,
        )

    # Late double-launch guard: closes the TOCTOU window between the early bind
    # probe above and the uvicorn bind below (venv-vs-system-python race).
    guard_code = guard_double_launch(cfg)
    if guard_code != 0:
        return guard_code

    async def serve_both() -> None:
        await asyncio.gather(http_server.serve(), mcp_server.serve())

    # Lifecycle logging (state_dir/logs/service.jsonl): a "start" with no later
    # "stop"/"crash" is the fingerprint of an external kill (e.g. a Task Scheduler
    # ExecutionTimeLimit); a "crash" carries the traceback. This is what turns the
    # next "why did it die?" from a guess into a lookup.
    core.service_event(
        cfg, "start", version=__version__, pid=os.getpid(),
        host=cfg.bind_host, http_port=cfg.bind_http_port, mcp_port=cfg.bind_mcp_port,
    )
    try:
        asyncio.run(serve_both())
    except KeyboardInterrupt:
        core.service_event(cfg, "stop", reason="keyboard_interrupt")
        return 130
    except Exception as e:
        # An unhandled server exception would otherwise exit silently (stdout/stderr
        # are not captured by the scheduled task). Record it before re-raising.
        core.service_event(
            cfg, "crash", error=f"{type(e).__name__}: {e}",
            traceback=traceback.format_exc(),
        )
        raise
    core.service_event(cfg, "stop", reason="serve_returned")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
