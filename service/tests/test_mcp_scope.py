"""Per-tool scope enforcement at the MCP dispatch site.

auth.DEFAULT_SCOPE_RULES requires only `read` at the /mcp transport (initialize
and tools/list are read-shaped) and defers per-tool refinement to dispatch.
Without that refinement a `read` token could drive every write/destructive
tool — the HTTP twins correctly require `deploy`. These tests pin the
refinement so the two surfaces cannot diverge.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.lowlevel.server import request_ctx

from service import core
from service.mcp_app import (
    ScopeInsufficient,
    _authenticated_token_scope,
    _required_tool_scope,
    make_mcp,
)


def test_required_tool_scope_maps_read_vs_write(cfg: core.Config) -> None:
    mcp = make_mcp(cfg)
    # read-only tools need `read`
    assert _required_tool_scope(mcp, "optix_health") == "read"
    assert _required_tool_scope(mcp, "optix_describe_node") == "read"
    # mutating (write / destructive) tools need `deploy`
    assert _required_tool_scope(mcp, "optix_bridge_set_property") == "deploy"
    assert _required_tool_scope(mcp, "optix_bridge_delete_node") == "deploy"
    # unknown / annotation-less fails closed to the most restrictive scope
    assert _required_tool_scope(mcp, "does_not_exist") == "deploy"


def test_authenticated_token_scope_resolves_from_request_scope() -> None:
    # No request context set -> not token-authenticated -> None.
    assert _authenticated_token_scope() is None
    # Request present but no forwarded scope key (e.g. auth off) -> None.
    fake = SimpleNamespace(request=SimpleNamespace(scope={}))
    tok = request_ctx.set(fake)
    try:
        assert _authenticated_token_scope() is None
    finally:
        request_ctx.reset(tok)
    # Forwarded scope key present -> returned verbatim.
    fake2 = SimpleNamespace(request=SimpleNamespace(scope={"ftxm.token_scope": "deploy"}))
    tok2 = request_ctx.set(fake2)
    try:
        assert _authenticated_token_scope() == "deploy"
    finally:
        request_ctx.reset(tok2)


def _call(mcp, name, args):
    return asyncio.run(mcp._tool_manager.call_tool(name, args))


def test_read_token_cannot_call_write_tool(cfg: core.Config) -> None:
    mcp = make_mcp(cfg)
    fake = SimpleNamespace(request=SimpleNamespace(scope={"ftxm.token_scope": "read"}))
    tok = request_ctx.set(fake)
    try:
        with pytest.raises(ScopeInsufficient):
            _call(mcp, "optix_bridge_delete_node", {"path": "UI/Screen1/Btn"})
    finally:
        request_ctx.reset(tok)


def test_unauthenticated_dispatch_is_not_scope_gated(cfg: core.Config) -> None:
    """Auth-off default: no forwarded token scope, so the gate is skipped and a
    read-only tool dispatches normally (no ScopeInsufficient)."""
    mcp = make_mcp(cfg)
    # No request_ctx set -> _authenticated_token_scope() is None -> no gate.
    out = _call(mcp, "optix_list_skills", {})
    # call_tool returns content/tuple; the point is it did NOT raise.
    assert out is not None
