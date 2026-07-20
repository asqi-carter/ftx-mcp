"""Canonical Optix node templates — export-safe, render-proven shapes.

Authored at column 0 (`- Name:` flush left); the caller reindents to the
target node's child depth via optix_model.reindent. Property values are
Optix's inline shorthand (Left/Top/Text/...) which Studio expands on import —
the exact hybrid the demo recipe deployed: inline statics + an expanded
`Children:` block ONLY where a DynamicLink binding is needed (bindings require
the child-variable + References form; the shorthand cannot express them).

Source of truth: live-validated authoring sessions (Switch + Label +
Model.PowerOn, deployed live) and the management HMI tree under apps/.
"""
from __future__ import annotations

import uuid


def new_guid() -> str:
    """Optix node Id: `g=` + 32 lowercase hex. Unique within a file."""
    return "g=" + uuid.uuid4().hex


def _dq(s: str) -> str:
    """Double-quote a scalar for Optix YAML, escaping backslash and quote."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _num(v: float | int) -> str:
    """Render a position/size as Optix does: floats keep a decimal."""
    if isinstance(v, bool):  # guard: bool is an int subclass
        raise TypeError("position/size must be a number, not bool")
    if isinstance(v, int):
        return f"{float(v)}"
    return f"{v}"


def _binding_child(prop: str, target: str) -> list[str]:
    """A `Children:`-block binding one Boolean property via DynamicLink.

    target is an Optix node path, e.g. "{Model}/PowerOn".
    """
    return [
        "  Children:",
        f"  - Name: {prop}",
        "    Type: BaseDataVariableType",
        "    DataType: Boolean",
        "    References:",
        f'    - {{Type: HasDynamicLink, Target: "{target}"}}',
    ]


def label(
    name: str,
    text: str,
    left: float = 100.0,
    top: float = 100.0,
    width: float = 200.0,
    height: float = 30.0,
    text_color: str | None = None,
    font_size: float | None = None,
    visible_bind: str | None = None,
) -> str:
    """A Label. `visible_bind` (e.g. "{Model}/PowerOn") binds Visible to a
    Boolean node; omit for an always-visible static label."""
    lines = [
        f"- Name: {name}",
        f"  Id: {new_guid()}",
        "  Type: Label",
        f"  Width: {_num(width)}",
        f"  Height: {_num(height)}",
        f"  Left: {_num(left)}",
        f"  Top: {_num(top)}",
        "  HorizontalAlignment: Center",
        "  TextHorizontalAlignment: Center",
        f"  Text: {_dq(text)}",
    ]
    if text_color is not None:
        lines.append(f"  TextColor: {_dq(text_color)}")
    if font_size is not None:
        lines += ["  Font:", f"    Size: {_num(font_size)}"]
    if visible_bind is not None:
        lines += _binding_child("Visible", visible_bind)
    return "\n".join(lines)


def switch(
    name: str,
    checked_bind: str,
    left: float = 100.0,
    top: float = 100.0,
    width: float = 80.0,
    height: float = 40.0,
) -> str:
    """A Switch whose Checked is bound (read+write) to a Boolean node.
    `checked_bind` e.g. "{Model}/PowerOn"."""
    lines = [
        f"- Name: {name}",
        f"  Id: {new_guid()}",
        "  Type: Switch",
        f"  Width: {_num(width)}",
        f"  Height: {_num(height)}",
        f"  Left: {_num(left)}",
        f"  Top: {_num(top)}",
    ]
    lines += _binding_child("Checked", checked_bind)
    return "\n".join(lines)


def boolean_var(name: str) -> str:
    """A Boolean Model variable — the bind target a Switch writes and a
    Label's Visible reads.

    EXPORT-SAFETY: this is deliberately the BARE
    shape — `Name` + `Type` + `DataType`, nothing else. On FactoryTalk
    SettingsWidget-template projects, adding a model variable carrying
    `AccessLevel` / `UserAccessLevel` (and `Id` / `Value`) via file edit makes
    `FTOptixStudio.exe export` HANG indefinitely — the deploy times out, gets
    tree-killed, and every later export of that project hangs until the
    offending variable is removed. Bare variables matching the project's own
    Studio-authored shape export cleanly (~12s). The demo's fuller shape
    (Id + AccessLevel 3 + Value) worked on a small scratch project but not
    on these templates.

    Runtime-writability of a bare variable from a Switch is the open question
    the demo's explicit `AccessLevel: 3` was hedging — verify at runtime
    before relying on a bound toggle on a template project.
    """
    return "\n".join([
        f"- Name: {name}",
        "  Type: BaseDataVariableType",
        "  DataType: Boolean",
    ])


# Widget kind -> builder. add_widget dispatches on this.
WIDGET_BUILDERS = {
    "label": label,
    "switch": switch,
}
