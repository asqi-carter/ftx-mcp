"""Process entrypoint: serves FastAPI HTTP on :8765 and FastMCP HTTP/SSE on :8766.

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
import socket
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn

from . import __version__, core
from .auth import AuthMiddleware, TokenStore
from .http_app import make_app
from .mcp_app import make_mcp


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


def main(argv: list[str] | None = None) -> int:
    cfg = core.Config.from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

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

    http_app: Any = make_app(cfg)
    mcp = make_mcp(cfg)
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
    print(f"  MCP   http://{cfg.bind_host}:{cfg.bind_mcp_port}/mcp", flush=True)
    print(f"  state {cfg.state_dir}", flush=True)
    if cfg.auth_required:
        print(f"  auth  required (tokens loaded: {len(store)})", flush=True)
    else:
        print("  auth  disabled (loopback only)", flush=True)
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

    async def serve_both() -> None:
        await asyncio.gather(http_server.serve(), mcp_server.serve())

    try:
        asyncio.run(serve_both())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
