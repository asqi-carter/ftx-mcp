"""Indentation-aware locator for Optix node YAML — NO full parse.

Optix `Nodes/**/*.yaml` is 2-space-indented node YAML with custom inline
forms (`Id: g=<hex>`, `{Type: HasDynamicLink, ...}` flow maps,
`{"NamespaceIndex":18,...}` JSON scalars). A parse-and-dump round-trip would
reformat the whole file — and Optix silently renders a reformatted/misshaped
node as transparent with no error (see the project's color-converter notes).
So the granular edit tools NEVER parse-and-dump: they locate a line
structurally here, then hand a byte-exact anchored insert/replace to the edit
edit engine, leaving every untouched line identical.

Two node forms appear:

  Root mapping (the file's top node):        List item (a child node):
    Name: Model            <- indent 0          - Name: Screen1     <- '-' at col L
    Type: ModelFolderType  <- indent 0            Type: Screen      <- col L+2
    Children:              <- indent 0            Children:        <- col L+2
    - Name: SomeVar        <- indent 0            - Name: Btn      <- col L+2

In both, `Children:` and the child `- Name:` items sit at the SAME column —
the node's "body indent". For a root mapping that equals the Name indent; for
a list item it is the dash column + 2. NodeSpan.body_indent captures it, and
both child placement and template reindentation key off it (NOT a fixed +2).

Analysis runs on EOL-agnostic `splitlines()`; anchors are returned as
'\\n'-joined slices of the ORIGINAL lines and the edit engine normalizes them
back to the file's CRLF/LF at apply time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Node Types that count as a placeable screen/panel surface. ScreensCategoryFolder,
# ScreenStyle, etc. are deliberately excluded — they are not screens.
SCREEN_TYPES = ("Screen", "Panel", "Dialog")

# Matches a node header in either form; `dash` is present for list items.
_NAME_RE = re.compile(r"^(?P<lead> *)(?P<dash>- )?Name:\s*(?P<name>.+?)\s*$")
_TYPE_RE = re.compile(r"^ *Type:\s*(?P<type>\S+)\s*$")


def _leading(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


@dataclass
class NodeSpan:
    name: str
    node_type: str | None
    body_indent: int    # column of Children: and of child '- Name:' items
    start: int          # line index of the Name header
    end: int            # exclusive end of the node's block
    children_line: int | None  # index of the body-indent 'Children:' line, if present


def _scan_node(lines: list[str], i: int, m: re.Match) -> NodeSpan:
    """Build a NodeSpan for a node header matched at line i."""
    is_list = bool(m.group("dash"))
    lead = len(m.group("lead"))
    body_indent = lead + 2 if is_list else lead
    # Block boundary: a list item ends at the next line whose indent <= its
    # dash column; a root mapping ends at a line that dedents strictly below
    # its own indent (its sibling keys share the indent and stay in-block).
    floor = lead if is_list else lead - 1
    end = len(lines)
    for j in range(i + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and _leading(ln) <= floor:
            end = j
            break
    node_type = None
    children_line = None
    children_re = re.compile(rf"^ {{{body_indent}}}Children:\s*$")
    for j in range(i + 1, end):
        if node_type is None:
            tm = _TYPE_RE.match(lines[j])
            if tm:
                node_type = tm.group("type")
        if children_line is None and children_re.match(lines[j]):
            children_line = j
    return NodeSpan(
        name=m.group("name").strip().strip("'\""),
        node_type=node_type,
        body_indent=body_indent,
        start=i,
        end=end,
        children_line=children_line,
    )


def find_node(lines: list[str], name: str, type_filter: tuple[str, ...] | None = None) -> NodeSpan | None:
    """First node literally named `name` (optionally Type in type_filter)."""
    target = name.strip().strip("'\"")
    for i, ln in enumerate(lines):
        m = _NAME_RE.match(ln)
        if not m or m.group("name").strip().strip("'\"") != target:
            continue
        span = _scan_node(lines, i, m)
        if type_filter and span.node_type not in type_filter:
            continue
        return span
    return None


def list_screens(lines: list[str]) -> list[dict]:
    """Every Screen/Panel/Dialog node in the file, with a child count."""
    out: list[dict] = []
    for i, ln in enumerate(lines):
        m = _NAME_RE.match(ln)
        if not m:
            continue
        span = _scan_node(lines, i, m)
        if span.node_type not in SCREEN_TYPES:
            continue
        child_count = 0
        if span.children_line is not None:
            for j in range(span.children_line + 1, span.end):
                cl = lines[j]
                if _leading(cl) == span.body_indent and cl.lstrip().startswith("- "):
                    child_count += 1
        out.append({
            "name": span.name,
            "type": span.node_type,
            "line": i + 1,
            "child_count": child_count,
        })
    return out


def reindent(block: str, spaces: int) -> str:
    """Shift a template block (authored at column 0) right by `spaces`.
    Blank lines stay blank (no trailing whitespace)."""
    pad = " " * spaces
    return "\n".join(pad + ln if ln.strip() else "" for ln in block.split("\n"))


def plan_first_child(lines: list[str], node: NodeSpan, child_block_col0: str) -> dict:
    """Edit fields (path-less) that add `child_block_col0` as the FIRST
    child of `node`, handling all three Children states a real project shows:

      1. `Children:` block present  -> insert after it.
      2. `Children: []` inline-empty -> convert to a block, then add the child.
      3. no `Children:` at all       -> append `Children:` + the child.

    A fresh Optix project's Model folder is state 3 and a blank screen is
    state 2 — both must work to edit a project from empty, so the granular
    tools route every case through here instead of requiring a pre-existing
    Children block.
    """
    bi = node.body_indent
    reindented = reindent(child_block_col0, bi)

    # Case 1: a body-indent `Children:` block line already exists.
    if node.children_line is not None:
        anchor = "\n".join(lines[node.start : node.children_line + 1])
        return {"insert_after_anchor": anchor, "block": reindented}

    # Case 2: `Children: []` (or `Children: {}`) inline-empty at body indent.
    empty_re = re.compile(rf"^ {{{bi}}}Children:\s*(\[\s*\]|\{{\s*\}})\s*$")
    for j in range(node.start + 1, node.end):
        if empty_re.match(lines[j]):
            head = lines[node.start : j]
            find = "\n".join(lines[node.start : j + 1])
            replace = "\n".join([*head, " " * bi + "Children:", reindented])
            return {"find": find, "replace": replace, "expect_count": 1}

    # Case 3: no Children at all — append after the node's last non-blank line.
    last = node.end - 1
    while last > node.start and not lines[last].strip():
        last -= 1
    find = "\n".join(lines[node.start : last + 1])
    replace = "\n".join([*lines[node.start : last + 1], " " * bi + "Children:", reindented])
    return {"find": find, "replace": replace, "expect_count": 1}
