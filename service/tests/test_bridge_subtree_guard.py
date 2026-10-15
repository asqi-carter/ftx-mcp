"""Source-level regression tests for the bridge subtree pre-check (PRD 0019)."""
from __future__ import annotations

import re
from pathlib import Path

_CS = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"


def _src() -> str:
    return _CS.read_text(encoding="utf-8", errors="ignore")


def _body(text: str, signature: str) -> str:
    start = text.index(signature)
    nxt = text.find("\n    private ", start + len(signature))
    return text[start:nxt if nxt > 0 else len(text)]


def test_helper_detects_netlogic_and_event_handler():
    body = _body(_src(), "private string FindUnsafeSubtreeAt(")
    assert "IsNetLogicNode(node)" in body
    assert "IsEventHandlerNode(node)" in body
    assert "depth > 12" in body
    assert '"netlogic"' in body and '"event_handler"' in body


def test_move_checks_subtree_before_make_object():
    body = _body(_src(), "private string MoveNodeInline(")
    assert body.index("SubtreeRefusal(") < body.index("MakeObject(")
    assert body.index("SubtreeRefusal(") < body.index("DupNameGuard(")


def test_convert_checks_subtree_before_make_object():
    body = _body(_src(), "private string ConvertToTypeInline(")
    assert body.index("SubtreeRefusal(") < body.index("MakeObjectType(")


def test_batch_move_uses_subtree_helper():
    text = _src()
    start = text.index('case "move":')
    seg = text[start:start + 4000]
    assert "FindUnsafeSubtree(live" in seg


def test_error_codes_present():
    text = _src()
    assert "refused_netlogic_move" in text
    assert '"contains_netlogic"' in text
    assert '"contains_event_handler"' in text
    assert "Studio's own rename" in text


def test_bridge_version_bumped_and_advertised():
    text = _src()
    m = re.search(r'BridgeVersion\s*=\s*"([^"]+)"', text)
    assert m and tuple(map(int, m.group(1).split("."))) >= (1, 0, 9)
    assert '\\"subtree_guard\\"' in text
    core = (_CS.parents[1] / "service" / "core.py").read_text(encoding="utf-8")
    assert "subtree_guard" in core
