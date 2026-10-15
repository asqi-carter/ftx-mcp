"""Source-level guard for the bridge C# (no dotnet needed). PRD 0029, #26."""
from __future__ import annotations

from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"

PRD_POINTER = "see the SDK facts in docs/prd/0029-bridge-compile-fix.md (PRD 0029)"

# Exact tokens only: ``RemoveTranslationInline(`` and ``node.ModellingRule`` must not match.
FORBIDDEN_TOKENS = ("RemoveTranslation(", "typeof(ModellingRule)", "(ModellingRule)")


def find_forbidden(src: str) -> list[str]:
    """Return the forbidden bridge tokens present in ``src`` (exact-token match)."""
    found = [t for t in FORBIDDEN_TOKENS if t in src]
    return found


def test_bridge_source_has_no_forbidden_tokens() -> None:
    """The bridge must not call ``RemoveTranslation(`` nor use a ``ModellingRule`` type."""
    src = BRIDGE.read_text(encoding="utf-8")
    found = find_forbidden(src)
    assert not found, (
        f"#26: StudioMCPBridge.cs contains forbidden token(s) {found}. "
        "The SDK has no RemoveTranslation method and no 'ModellingRule' type (CS0246/CS1061); "
        f"IUANode.ModellingRule is a UAManagedCore.NamingRuleType - {PRD_POINTER}"
    )


def test_modelling_rule_cast_goes_through_naming_rule_type() -> None:
    """``IUANode.ModellingRule`` is a ``UAManagedCore.NamingRuleType``; no ``ModellingRule`` type exists."""
    src = BRIDGE.read_text(encoding="utf-8")
    assert "(NamingRuleType)Enum.Parse(typeof(NamingRuleType), rule)" in src, (
        f"#26: set_modelling_rule must cast through NamingRuleType - {PRD_POINTER}"
    )


def test_guard_does_not_flag_property_access() -> None:
    """``node.ModellingRule`` (valid property access) and ``*Inline(`` helpers are not flagged."""
    ok = "string p = node.ModellingRule.ToString(); node.ModellingRule = x; RemoveTranslationInline(a);"
    assert find_forbidden(ok) == []
    assert find_forbidden("n.RemoveTranslation(k);") == ["RemoveTranslation("]
    assert find_forbidden("var t = typeof(ModellingRule);") == ["typeof(ModellingRule)", "(ModellingRule)"]
    assert find_forbidden("var r = (ModellingRule)x;") == ["(ModellingRule)"]
