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

from service import auth, core
from service.mcp_app import (
    ScopeInsufficient,
    _authenticated_token_scope,
    _required_tool_scope,
    make_mcp,
)


def test_required_tool_scope_maps_the_four_tiers(cfg: core.Config) -> None:
    mcp = make_mcp(cfg)
    # health-tier liveness probe
    assert _required_tool_scope(mcp, "optix_runtime_status") == "health"
    # read-only introspection
    assert _required_tool_scope(mcp, "optix_describe_node") == "read"
    # author-tier: mutate the Studio project (was `deploy` under the old
    # annotation-derived binary split) — set-property and destructive
    # delete-node both mutate the project model, not the runtime.
    assert _required_tool_scope(mcp, "optix_bridge_set_property") == "author"
    assert _required_tool_scope(mcp, "optix_bridge_delete_node") == "author"
    # deploy-tier: pushes to / controls the runtime
    assert _required_tool_scope(mcp, "optix_deploy") == "deploy"
    assert _required_tool_scope(mcp, "optix_runtime_start") == "deploy"
    # unknown / unclassified fails closed to the most restrictive scope
    assert _required_tool_scope(mcp, "does_not_exist") == "deploy"


def test_every_registered_tool_has_a_scope_entry(cfg: core.Config) -> None:
    """Completeness control: every registered tool is classified in
    auth.TOOL_SCOPES and resolves to a real scope. A future tool added
    without a table entry silently falls through to the `deploy` default —
    this red build forces a conscious scope decision instead."""
    mcp = make_mcp(cfg)
    unclassified = []
    bad_scope = []
    for name in mcp._tool_manager._tools:
        if name not in auth.TOOL_SCOPES:
            unclassified.append(name)
        if _required_tool_scope(mcp, name) not in auth.SCOPES:
            bad_scope.append(name)
    assert unclassified == [], (
        f"tools with no auth.TOOL_SCOPES entry (would fall through to the "
        f"deploy default): {unclassified} — add each to the table"
    )
    assert bad_scope == [], f"tools resolving to a non-scope value: {bad_scope}"


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
    # `author` round-trips verbatim too (cheap regression pin for the new tier).
    fake3 = SimpleNamespace(request=SimpleNamespace(scope={"ftxm.token_scope": "author"}))
    tok3 = request_ctx.set(fake3)
    try:
        assert _authenticated_token_scope() == "author"
    finally:
        request_ctx.reset(tok3)


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


def test_author_token_cannot_call_deploy_tool(cfg: core.Config) -> None:
    """The new tier's headline guarantee: an `author` token can drive every
    authoring tool but is refused at the runtime-push boundary. The gate
    raises before dispatch, so optix_deploy's body never runs."""
    mcp = make_mcp(cfg)
    fake = SimpleNamespace(request=SimpleNamespace(scope={"ftxm.token_scope": "author"}))
    tok = request_ctx.set(fake)
    try:
        with pytest.raises(ScopeInsufficient):
            _call(mcp, "optix_deploy", {"project": "TestProj"})
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


# ---- optix_bridge_edit scope pin ---------------------------

def test_bridge_edit_scope_is_author() -> None:
    """optix_bridge_edit must stay in the 'author' tier of auth.TOOL_SCOPES.

    It dispatches the same bridge writes as the individual per-noun tools
    (set_property, bind, create_widget, ...), so it must not widen to 'deploy'
    (which would block live-authoring tokens from using it) and must not narrow
    to 'read' (which would let read-only tokens drive model mutations).

    Static pin — a change to this scope must pass a conscious review, not
    slip through as a side-effect of table reordering.
    """
    assert auth.TOOL_SCOPES["optix_bridge_edit"] == "author", (
        f"optix_bridge_edit scope changed from 'author' to "
        f"{auth.TOOL_SCOPES.get('optix_bridge_edit')!r}; "
        "update auth.TOOL_SCOPES only after reviewing the impact on "
        "live-authoring tokens"
    )
