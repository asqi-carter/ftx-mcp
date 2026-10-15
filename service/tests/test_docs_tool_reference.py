"""docs/tool-reference.md names only actions/ops the code really has (#84).

The doc tables name tool actions (``optix_project`` ``action="open"``/``"new"``)
and the batch verbs of ``optix_bridge_edit``. These tests parse the doc and
check every such name against the code's registries: the tool ``action`` /
``mode`` Literal enums on the registered MCP tools and ``core.BRIDGE_EDIT_VERBS``
(the ``valid_ops`` list). A doc that drifts (``"create"`` for ``"new"``) fails.
"""
from __future__ import annotations

import re
from pathlib import Path

from service import core
from service.mcp_app import make_mcp
from service.tests.test_cdp_consolidated import _tool_fn  # noqa: F401  (same harness)

_DOC = Path(__file__).resolve().parents[2] / "docs" / "tool-reference.md"
_DISCRIMINATORS = ("action", "mode")
_MCP_SRC = (Path(__file__).resolve().parents[1] / "mcp_app.py").read_text(encoding="utf-8")


def _doc_text() -> str:
    return _DOC.read_text(encoding="utf-8")


def _doc_tool_actions() -> list[tuple[str, str, str]]:
    """(tool, param, value) for every ``optix_x`` (`action="a"`/`"b"`) in a table row."""
    found: list[tuple[str, str, str]] = []
    for line in _doc_text().splitlines():
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1]
        for m in re.finditer(
                r"`(optix_\w+)`\s*\(((?:`?(?:action|mode)=)?\"[^)]*)\)", first_cell):
            tool, inner = m.group(1), m.group(2)
            pm = re.search(r"(action|mode)=", inner)
            param = pm.group(1) if pm else "action"
            for val in re.findall(r"\"([A-Za-z_]+)\"", inner):
                found.append((tool, param, val))
    return found


def _doc_batch_verbs() -> list[str]:
    """Backticked verbs in the ``optix_bridge_edit`` verb list paragraph."""
    text = _doc_text()
    start = text.index("`optix_bridge_edit` is the primary entry point")
    end = text.index("validated as a whole", start)
    return re.findall(r"`([a-z_]+)`", text[start:end])[1:]  # [0] is the tool name


def _registered_enums(cfg) -> dict[str, dict[str, set[str]]]:
    mcp = make_mcp(cfg)
    out: dict[str, dict[str, set[str]]] = {}
    for tool in mcp._tool_manager.list_tools():
        props = (tool.parameters or {}).get("properties", {})
        for p in _DISCRIMINATORS:
            enum = props.get(p, {}).get("enum")
            if enum:
                out.setdefault(tool.name, {})[p] = set(enum)
    return out


def test_every_action_and_op_name_in_tool_reference_exists_in_code(cfg):
    """Every action/mode and batch verb the tool-reference tables name exists."""
    enums = _registered_enums(cfg)
    registered = {t.name for t in make_mcp(cfg)._tool_manager.list_tools()}
    actions = _doc_tool_actions()
    assert actions, "parser found no `tool (action=...)` rows in tool-reference.md"

    problems: list[str] = []
    for tool, param, val in actions:
        valid = enums.get(tool, {}).get(param)
        if valid is None:
            # `str`-typed discriminator (optix_bridge_edit.action): the tool
            # must still be registered and dispatch on that literal in code.
            if tool not in registered or f'"{val}"' not in _MCP_SRC:
                problems.append(f"{tool}: documented {param}={val!r} but the tool "
                                f"has no {param} Literal (or is not registered)")
        elif val not in valid:
            problems.append(f"{tool}: documented {param}={val!r} not in {sorted(valid)}")

    verbs = _doc_batch_verbs()
    assert len(verbs) >= 20, f"verb-list parser found too little: {verbs}"
    for v in verbs:
        if v not in core.BRIDGE_EDIT_VERBS:
            problems.append(f"optix_bridge_edit: documented verb {v!r} not in valid_ops")

    assert not problems, "\n".join(problems)


def test_tool_reference_batch_verb_list_is_complete():
    """The doc's batch verb list names every verb the code accepts (incl. the
    newer import_tags / convert_to_type / create_netlogic)."""
    documented = set(_doc_batch_verbs())
    assert {"import_tags", "convert_to_type", "create_netlogic"} <= documented
    missing = sorted(core.BRIDGE_EDIT_VERBS - documented)
    assert not missing, f"verbs accepted by the code but missing from the doc: {missing}"


def test_tool_reference_optix_project_actions_are_open_and_new(cfg):
    """optix_project is documented with open/new (not create) and the code agrees."""
    doc = {v for t, _p, v in _doc_tool_actions() if t == "optix_project"}
    assert doc == {"open", "new"}
    assert _registered_enums(cfg)["optix_project"]["action"] == {"open", "new"}


def test_tool_reference_unknown_batch_verb_is_bad_op_verb_not_unknown_op():
    """Docs: a bad verb is refused with `bad_op_verb`; `unknown_op` is only the
    validator's 'applied unchecked' warning."""
    text = _doc_text()
    assert re.search(r"\| `bad_op_verb` \| error", text)
    assert re.search(r"\| `unknown_op` \| warning", text)
    assert not re.search(r"\| `unknown_op` \| error", text)


def test_tool_reference_documents_no_port_parameter_on_cdp_and_emulator_tools(cfg):
    """optix_emulator / optix_observe / optix_interact have no `port` parameter,
    and the doc must not claim one."""
    mcp = make_mcp(cfg)
    for tool in mcp._tool_manager.list_tools():
        if tool.name in ("optix_emulator", "optix_observe", "optix_interact"):
            assert "port" not in (tool.parameters or {}).get("properties", {}), tool.name
    for line in _doc_text().splitlines():
        if re.match(r"\| `optix_(emulator|observe|interact)`", line):
            assert "`port` parameter overrides" not in line, line[:60]
