"""Bridge arming + Studio CLI (v1.0.7).

The UIA gesture itself can only be exercised on a Windows box with Studio open
(commissioned by hand), so these cover the parts that CAN be tested off-box and
that carry the real regression risk: YAML-derived navigation, the
identity-based fast exit, and the refusals.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from service import core, studio_arm

STOCK = textwrap.dedent("""\
    Name: NetLogic
    Type: NetLogicCategoryFolder
    Children:
    - Name: StudioMCPBridge
      Type: NetLogic
      Children:
      - Name: BehaviourStartPriority
        Type: BehaviourStartPriorityVariableType
        Value: 180
      - Class: Method
        Name: CheckFormula
      - Class: Method
        Name: StartBridge
      - Class: Method
        Name: StopBridge
    """)

# Customer forks nest the bridge under extra folders; the chain must follow
# the file rather than a hardcoded default.
NESTED = textwrap.dedent("""\
    Name: NetLogic
    Type: NetLogicCategoryFolder
    Children:
    - Name: Misc.
      Type: Folder
      Children:
      - Name: DesignTimeNetLogic
        Type: Folder
        Children:
        - Name: StudioMCPBridge
          Type: NetLogic
          Children:
          - Name: BehaviourStartPriority
            Value: 180
          - Class: Method
            Name: StartBridge
    """)


def _yaml(tmp_path: Path, text: str) -> str:
    p = tmp_path / "NetLogic.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_derive_chain_stock_layout(tmp_path: Path) -> None:
    assert studio_arm.derive_chain(_yaml(tmp_path, STOCK), "StartBridge") == [
        "NetLogic", "StudioMCPBridge"]


def test_derive_chain_excludes_siblings(tmp_path: Path) -> None:
    """BehaviourStartPriority sits at the SAME depth as the method entries, so
    a naive indent walk pulls it into the chain. It is a sibling subtree, not
    an ancestor — including it sends the gesture at the wrong row."""
    chain = studio_arm.derive_chain(_yaml(tmp_path, STOCK), "StartBridge")
    assert "BehaviourStartPriority" not in chain


def test_derive_chain_keeps_the_folder_root(tmp_path: Path) -> None:
    """The root folder is the MOST useful row (right-clicking a folder lists
    its children's Execute entries, so the leaf never needs expanding). A
    list-item indent bug drops it, which is why this is pinned."""
    assert studio_arm.derive_chain(_yaml(tmp_path, STOCK), "StartBridge")[0] == "NetLogic"


def test_derive_chain_follows_nested_layout(tmp_path: Path) -> None:
    assert studio_arm.derive_chain(_yaml(tmp_path, NESTED), "StartBridge") == [
        "NetLogic", "Misc.", "DesignTimeNetLogic", "StudioMCPBridge"]


def test_derive_chain_unknown_method_is_none(tmp_path: Path) -> None:
    assert studio_arm.derive_chain(_yaml(tmp_path, STOCK), "NopeMethod") is None


def test_derive_chain_missing_file_is_none(tmp_path: Path) -> None:
    assert studio_arm.derive_chain(str(tmp_path / "nope.yaml")) is None


def test_find_bridge_yaml(tmp_path: Path) -> None:
    nodes = tmp_path / "Nodes" / "NetLogic"
    nodes.mkdir(parents=True)
    (nodes / "NetLogic.yaml").write_text(STOCK, encoding="utf-8")
    assert studio_arm.find_bridge_yaml(tmp_path).endswith("NetLogic.yaml")


def test_find_bridge_yaml_absent(tmp_path: Path) -> None:
    (tmp_path / "Nodes").mkdir()
    assert studio_arm.find_bridge_yaml(tmp_path) is None


def test_serving_port_for_matches_project_not_port(monkeypatch) -> None:
    """THE multi-instance invariant: the question is 'is a bridge serving THIS
    project', never 'is the base port open'. With another Studio already on
    8768, asking the port question reports success having armed nothing."""
    monkeypatch.setattr(studio_arm, "bound_ports", lambda *a, **k: {8768, 8769})
    monkeypatch.setattr(studio_arm, "_bridge_health",
                        lambda p, timeout=0.5: (
                            {"project": {8768: "Other", 8769: "Mine"}[p]}
                            if p in (8768, 8769) else None))
    assert studio_arm.serving_port_for("Mine") == 8769
    assert studio_arm.serving_port_for("Nobody") is None


def test_arm_fast_exit_is_per_project(monkeypatch) -> None:
    monkeypatch.setattr(studio_arm, "serving_port_for", lambda *a, **k: 8769)
    out = studio_arm.execute_method("Mine", "/nope", method="StartBridge")
    assert out["ok"] and out["state"] == "already_armed" and out["port"] == 8769


def test_stop_on_an_unarmed_project_is_a_noop(monkeypatch) -> None:
    monkeypatch.setattr(studio_arm, "serving_port_for", lambda *a, **k: None)
    out = studio_arm.execute_method("Mine", "/nope", method="StopBridge")
    assert out["ok"] and out["state"] == "not_running"


def test_project_new_refuses_existing_directory(cfg: core.Config) -> None:
    """Never write into a populated directory — the CLI would decide for us."""
    (cfg.projects_root / "Taken").mkdir(parents=True, exist_ok=True)
    out = core.project_new(cfg, "Taken")
    assert out["ok"] is False and out["error"] == "already_exists"


@pytest.mark.parametrize("name", ["../escape", "sub/dir", "back\\slash"])
def test_project_new_rejects_path_like_names(cfg: core.Config, name: str) -> None:
    out = core.project_new(cfg, name)
    assert out["ok"] is False and out["error"] == "invalid_project_name"


def test_project_open_without_an_optix_file(cfg: core.Config) -> None:
    (cfg.projects_root / "Empty").mkdir(parents=True, exist_ok=True)
    out = core.project_open(cfg, "Empty")
    assert out["ok"] is False and out["error"] == "project_file_missing"


def test_project_new_polls_the_tree_not_process_exit(cfg: core.Config, monkeypatch) -> None:
    """Studio's CLI verbs are GUI launches, not batch commands: `new` creates
    the project then STAYS RUNNING with it open. Waiting for exit is wrong
    twice -- it blocks for the life of the editor, and Runner.run tree-kills on
    TimeoutExpired, so waiting actually kills the Studio the command started
    (measured: all 25 files created, then the timeout reaped the editor).
    Readiness must therefore be the .optix appearing on disk."""
    dest = cfg.projects_root / "Fresh"

    def fake_launch(_cfg, _args):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "Fresh.optix").write_text("project", encoding="utf-8")
        return {"ok": True, "pid": 4321}

    monkeypatch.setattr(core, "_studio_launch", fake_launch)
    monkeypatch.setattr(core, "_CREATE_GRACE_S", 0.0)  # grace is tested elsewhere
    out = core.project_new(cfg, "Fresh", wait_seconds=5)
    assert out["ok"] and out["state"] == "created"
    assert out["pid"] == 4321 and out["studio_left_open"] is True


def test_project_new_reports_timeout_without_the_optix(cfg: core.Config, monkeypatch) -> None:
    monkeypatch.setattr(core, "_studio_launch", lambda *a: {"ok": True, "pid": 1})
    out = core.project_new(cfg, "NeverLands", wait_seconds=0.2)
    assert out["ok"] is False and out["error"] == "create_timeout"


def test_project_open_surfaces_a_launch_failure(cfg: core.Config, monkeypatch) -> None:
    d = cfg.projects_root / "HasOptix"
    d.mkdir(parents=True, exist_ok=True)
    (d / "HasOptix.optix").write_text("Name: HasOptix", encoding="utf-8")
    (d / "IDEVersion.txt").write_text("1.8.0.555", encoding="utf-8")
    monkeypatch.setattr(core, "_studio_launch",
                        lambda *a: {"ok": False, "error": "studio_exe_missing"})
    out = core.project_open(cfg, "HasOptix", wait_seconds=1)
    assert out["ok"] is False and out["error"] == "studio_exe_missing"


def test_arm_names_a_missing_netlogic_distinctly(tmp_path, monkeypatch) -> None:
    """A freshly-created project has no bridge NetLogic. That must NOT read as
    a navigation miss -- the fix is to add the node, not to hunt the UI."""
    monkeypatch.setattr(studio_arm, "serving_port_for", lambda *a, **k: None)
    (tmp_path / "Nodes").mkdir()
    out = studio_arm.execute_method("Fresh", str(tmp_path), method="StartBridge")
    assert out["ok"] is False and out["error"] == "bridge_netlogic_absent"
    assert "StudioMCPBridge" in out["nudge"]


def test_liveness_is_http_never_a_bare_tcp_connect(monkeypatch) -> None:
    """REGRESSION: probing with a raw connect-then-close makes the bridge log
    'request error: ... connection was aborted by the software in your host
    machine' once per probe — 26 such WARNINGs were traced to exactly that in
    one afternoon. Liveness must go through /bridge/health, like
    core.list_bridges has always done."""
    import service.studio_arm as sa
    assert not hasattr(sa, "_port_open"), "bare-TCP probe reintroduced"
    seen: list[int] = []
    monkeypatch.setattr(sa, "_bridge_health",
                        lambda p, timeout=3.0: seen.append(p) or (
                            {"project": "P"} if p == 8769 else None))
    assert sa.bound_ports() == {8769}
    assert seen == [8768, 8769, 8770, 8771]


# ---- I9: unknown op fields must not be silently applied ------------------

def test_unknown_op_field_is_detected() -> None:
    """I9, reproduced on 1.0.7: a create_folder op carrying a bogus field
    returned applied:1 / errors:[] / warnings:[] / succeeded WITH strict:true,
    and the node was really created. Field notes call it 'the dangerous one'."""
    assert core.unknown_op_fields(
        {"op": "create_folder", "parent": "Model", "name": "X",
         "bogus_field_that_does_not_exist": "xyz"}) == [
        "bogus_field_that_does_not_exist"]


def test_legal_ops_are_clean() -> None:
    assert core.unknown_op_fields(
        {"op": "create_folder", "parent": "Model", "name": "X"}) == []
    assert core.unknown_op_fields(
        {"op": "bind", "path": "a", "name": "b",
         "source_path": "c", "mode": "Read"}) == []


def test_normalizer_aliases_are_not_flagged() -> None:
    """_normalize_edit_op accepts node_path/prop_name aliases, so they must not
    read as unknown — otherwise the check fires on valid callers."""
    assert core.unknown_op_fields(
        {"op": "set_property", "node_path": "a", "name": "b", "value": "c"}) == []


def test_unknown_verb_defers_to_the_bridge_validator() -> None:
    """Reporting it here too would give two errors for one mistake."""
    assert core.unknown_op_fields({"op": "no_such_verb", "whatever": 1}) == []


# ---- 2026-08-23: the first arm on a real nested project failed three ways ----
#
# A nested-layout fork opened fresh: menu_item_not_found, tried=[]. A project
# whose folder is not named after the project: studio_window_not_found. Measured causes: (1) the chain stops at the bridge's
# own yaml while the on-screen row is the parent folder declared one file up;
# (2) only the NetLogic CATEGORY folder lists Execute entries, so the tree must
# be expanded to it — via a click on the chevron 34px left of the row text
# ({Right} and double-click do nothing); (3) the bridge and the root row use
# Project.Current.BrowseName ("Line4"), not the directory name ("Line4_HMI").

FORK_MISC = "Name: Misc.\nType: FolderType\nChildren:\n- File: DesignTimeNetLogic/DesignTimeNetLogic.yaml\n"
FORK_DT = textwrap.dedent("""\
    Name: DesignTimeNetLogic
    Type: NetLogicCategoryFolder
    Children:
    - Name: SensorsScript
      Type: NetLogic
      Children:
      - Class: Method
        Name: CreateSensors
    - Name: StudioMCPBridge
      Type: NetLogic
      Children:
      - Name: BehaviourStartPriority
        Value: 180
      - Class: Method
        Name: StartBridge
      - Class: Method
        Name: StopBridge
    """)
# Flat shape: the bridge sits DIRECTLY under the plain Misc. folder, in
# that folder's own file.
HMI_MISC = textwrap.dedent("""\
    Name: Misc.
    Type: FolderType
    Children:
    - File: Security/Security.yaml
    - Name: StudioMCPBridge
      Type: NetLogic
      Children:
      - Class: Method
        Name: StartBridge
    """)


def _fork_tree(tmp_path: Path) -> Path:
    misc = tmp_path / "Nodes" / "Misc_"
    (misc / "DesignTimeNetLogic").mkdir(parents=True)
    (misc / "Misc_.yaml").write_text(FORK_MISC, encoding="utf-8")
    (misc / "DesignTimeNetLogic" / "DesignTimeNetLogic.yaml").write_text(FORK_DT, encoding="utf-8")
    (tmp_path / "Cell_v5.optix").write_text("", encoding="utf-8")
    return tmp_path


def test_folder_ancestors_follow_file_references(tmp_path: Path) -> None:
    _fork_tree(tmp_path)
    y = studio_arm.find_bridge_yaml(tmp_path)
    assert y.endswith("DesignTimeNetLogic.yaml")
    assert studio_arm.folder_ancestors(y) == ["Misc."]
    assert studio_arm.derive_chain(y) == ["DesignTimeNetLogic", "StudioMCPBridge"]


def test_folder_ancestors_stock_layout_is_empty(tmp_path: Path) -> None:
    nodes = tmp_path / "Nodes" / "NetLogic"
    nodes.mkdir(parents=True)
    (nodes / "NetLogic.yaml").write_text(STOCK, encoding="utf-8")
    assert studio_arm.folder_ancestors(str(nodes / "NetLogic.yaml")) == []


def test_bridge_directly_under_a_plain_folder(tmp_path: Path) -> None:
    misc = tmp_path / "Nodes" / "Misc_"
    misc.mkdir(parents=True)
    (misc / "Misc_.yaml").write_text(HMI_MISC, encoding="utf-8")
    y = studio_arm.find_bridge_yaml(tmp_path)
    # The folder's own file IS the bridge file: no ancestor is added twice.
    assert studio_arm.folder_ancestors(y) == []
    assert studio_arm.derive_chain(y) == ["Misc.", "StudioMCPBridge"]


def test_search_highlight_is_stripped_from_row_names() -> None:
    assert studio_arm._strip_tags("<b>StudioMCPBridge</b>") == "StudioMCPBridge"
    assert studio_arm._norm(" <b>Misc.</b> ") == "misc."
    assert studio_arm._strip_tags(None) == ""


def test_serving_port_for_accepts_browsename_aliases(monkeypatch) -> None:
    """Line4_HMI's bridge reports "Line4". Without the alias the arm would click
    Execute, then report verify_timeout while the bridge was already up."""
    monkeypatch.setattr(studio_arm, "bound_ports", lambda *a, **k: {8769})
    monkeypatch.setattr(studio_arm, "_bridge_health",
                        lambda p, timeout=0.5: {"project": "Line4"} if p == 8769 else None)
    assert studio_arm.serving_port_for("Line4_HMI") is None
    assert studio_arm.serving_port_for("Line4_HMI", aliases={"line4"}) == 8769


# -- a fake UIA surface: just enough of `uiautomation` for execute_method ----

class _Rect:
    def __init__(self, left, top, w, h):
        self.left, self.top, self._w, self._h = left, top, w, h

    def width(self):
        return self._w

    def height(self):
        return self._h


class _Ctl:
    def __init__(self, name, kind, rect=(0, 0, 0, 0), children=()):
        self.Name, self.ControlTypeName = name, kind
        self.BoundingRectangle = _Rect(*rect)
        self._children = list(children)
        self.ProcessId = 1

    def GetChildren(self):  # noqa: N802 -- mirrors uiautomation
        return list(self._children)

    def SetActive(self):  # noqa: N802 -- mirrors uiautomation
        return True


class _FocusableEdit(_Ctl):
    """Search box: reports keyboard focus only while the fake's last click
    landed on it (mirrors UIA HasKeyboardFocus)."""

    def __init__(self, studio, rect):
        super().__init__("", "EditControl", rect)
        self._studio = studio

    @property
    def HasKeyboardFocus(self):  # noqa: N802 -- mirrors uiautomation
        return self._studio.focus == "search"


class _FakeStudio:
    """One Studio window, half-screen at x=961 like the measured one, with
    `Misc.` COLLAPSED: `DesignTimeNetLogic` exists only after a click lands on
    the chevron 34px left of Misc.'s text. Right-clicking Misc. opens the plain
    folder menu; right-clicking DesignTimeNetLogic opens the category menu with
    the Execute entries. Clicking `Execute StartBridge` arms."""

    def __init__(self, root_row="Cell_v5", path_label=None, show_root=True,
                 search_box=False, nested=False):
        # nested=True is the 1.8 row shape: each tree label sits inside a
        # grouping child (ListItem > Group > Text) instead of directly under
        # the window (1.7).
        self.nested = nested
        self.root_row, self.path_label, self.show_root = root_row, path_label, show_root
        self.search_box = search_box
        self.expanded = False
        self.filter = ""
        self.focus = None
        self.menu = None
        self.armed = False
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[str] = []
        self.win = _Ctl("FactoryTalk Optix Studio", "WindowControl", (961, 0, 958, 1031))
        self.win.GetChildren = self._rows

    def _rows(self):
        rows = []
        if self.path_label:
            rows.append(_Ctl(self.path_label, "TextControl", (1084, 10, 499, 18)))
        if self.search_box:
            rows.append(_FocusableEdit(self, (961, 99, 349, 27)))
        if self.filter:
            # Filtered tree: the match with its ancestors, match bolded.
            rows.append(_Ctl(self.root_row, "TextControl", (1008, 126, 297, 28)))
            rows.append(_Ctl("Misc.", "TextControl", (1024, 154, 281, 28)))
            rows.append(_Ctl("DesignTimeNetLogic", "TextControl", (1040, 182, 265, 28)))
            rows.append(_Ctl(f"<b>{self.filter}</b>", "TextControl", (1056, 210, 249, 28)))
        else:
            if self.show_root:
                rows.append(_Ctl(self.root_row, "TextControl", (1008, 126, 297, 28)))
            rows.append(_Ctl("UI", "TextControl", (1024, 154, 281, 28)))
            rows.append(_Ctl("Misc.", "TextControl", (1024, 182, 281, 28)))
            y = 210
            if self.expanded:
                rows.append(_Ctl("DesignTimeNetLogic", "TextControl", (1040, y, 265, 28)))
                y += 28
            rows.append(_Ctl("Model", "TextControl", (1024, y, 281, 28)))
        # The Properties pane repeats the selected node's name, far right.
        rows.append(_Ctl("StudioMCPBridge", "TextControl", (1666, 99, 243, 28)))
        # Type-view tiles under the tree: same row height, overlapping x-band.
        # Measured to poison the indent estimate (1040 -> 1046 = "6 px").
        for i, n in enumerate(("User interface", "Model", "Security", "Database")):
            rows.append(_Ctl(n, "TextControl", (966 + 80 * i, 959, 70, 28)))
        if self.nested:
            rows = [self._group(c) if c.ControlTypeName == "TextControl"
                    and c.Name != self.path_label else c for c in rows]
        if self.menu is not None:
            rows.append(self.menu)
        return rows

    @staticmethod
    def _group(text):
        r = text.BoundingRectangle
        rect = (r.left, r.top, r.width(), r.height())
        inner = _Ctl("", "GroupControl", rect, [text])
        return _Ctl("", "ListItemControl", rect, [inner])

    def _row_at(self, x, y):
        for c in studio_arm._row_texts(self.win):
            r = c.BoundingRectangle
            if c.ControlTypeName == "TextControl" and r.left <= x < r.left + r.width() \
                    and r.top <= y < r.top + r.height():
                return c.Name
        return None

    def _menu(self, names):
        items = [_Ctl(n, "MenuItemControl", (1100, 300 + 22 * i, 220, 22))
                 for i, n in enumerate(names)]
        return _Ctl("", "MenuControl", (1100, 300, 220, 22 * len(names)), items)

    # --- module surface used by execute_method
    def GetRootControl(self):  # noqa: N802 -- mirrors uiautomation
        return _Ctl("", "PaneControl", children=[self.win])

    def GetCursorPos(self):  # noqa: N802 -- mirrors uiautomation
        return (0, 0)

    def SetCursorPos(self, x, y):  # noqa: N802 -- mirrors uiautomation
        pass

    def GetForegroundControl(self):  # noqa: N802 -- mirrors uiautomation
        return None

    def RightClick(self, x, y):  # noqa: N802 -- mirrors uiautomation
        row = self._row_at(x, y)
        row = studio_arm._strip_tags(row) if row else row  # bold leaf when filtered
        if row == "Misc.":
            self.menu = self._menu(["NodeSet", "Rename", "Delete", "New"])
        elif row == "DesignTimeNetLogic":
            self.menu = self._menu(["NodeSet", "Execute StopBridge",
                                    "Execute StartBridge", "Execute CreateSensors"])
        elif row == "StudioMCPBridge":
            # The leaf NetLogic's OWN menu lists its Execute entries too (measured
            # 2026-09-14: a 15-item leaf menu carried Execute StartBridge). It is
            # short, so it never clips -- which is why leaf-first is the default.
            self.menu = self._menu(["NodeSet", "Rename", "Delete", "New",
                                    "Execute StopBridge", "Execute StartBridge",
                                    "Execute SetupProject"])
        else:
            self.menu = None

    def Click(self, x, y):  # noqa: N802 -- mirrors uiautomation
        self.clicks.append((x, y))
        self.focus = "search" if (self.search_box and 99 <= y < 126 and x < 1310) else "tree"
        if self.menu is not None:
            for it in self.menu.GetChildren():
                r = it.BoundingRectangle
                if r.left <= x < r.left + r.width() and r.top <= y < r.top + r.height():
                    if it.Name == "Execute StartBridge":
                        self.armed = True
                    self.menu = None
                    return
        if not self.expanded and abs(x - (1024 - 34)) <= 4 and 182 <= y < 210:
            self.expanded = True

    def SendKeys(self, keys):  # noqa: N802 -- mirrors uiautomation
        self.keys.append(keys)
        if "{Esc}" in keys:
            self.menu = None
        elif self.focus == "search":
            if keys == "{Ctrl}a{Delete}":
                self.filter = ""
            elif not keys.startswith("{"):
                self.filter = keys


def _wire(monkeypatch, fake: _FakeStudio):
    monkeypatch.setitem(__import__("sys").modules, "uiautomation", fake)
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    monkeypatch.setattr(studio_arm, "bound_ports", lambda *a, **k: set())
    monkeypatch.setattr(studio_arm, "serving_port_for",
                        lambda *a, **k: 8768 if fake.armed else None)


def test_arm_expands_a_collapsed_tree_down_to_the_category_folder(tmp_path, monkeypatch) -> None:
    """No search box on offer: the chevron fallback. The indent must come
    from the tree rows above (16 px -> chevron at text-34), not from the
    Type-view tiles below, which would put the click on the folder icon."""
    _fork_tree(tmp_path)
    fake = _FakeStudio()
    _wire(monkeypatch, fake)
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["ok"] and out["state"] == "armed", out
    assert out["chain"] == ["Misc.", "DesignTimeNetLogic", "StudioMCPBridge"]
    assert out["expanded"] == ["Misc."]
    # The plain folder was tried (and offered no Execute), then the chevron
    # was clicked, then the category folder's menu carried the entry.
    assert [t["row"] for t in out["tried"]] == ["Misc.", "DesignTimeNetLogic"]
    assert (990, 196) in fake.clicks
    assert fake.expanded and fake.armed


def test_arm_expands_a_collapsed_tree_down_to_the_category_folder_18_shape(
        tmp_path, monkeypatch) -> None:
    """Same gesture as above with every label nested in a grouping child."""
    _fork_tree(tmp_path)
    fake = _FakeStudio(nested=True)
    _wire(monkeypatch, fake)
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["ok"] and out["state"] == "armed", out
    assert out["expanded"] == ["Misc."]
    assert [t["row"] for t in out["tried"]] == ["Misc.", "DesignTimeNetLogic"]
    assert (990, 196) in fake.clicks     # chevron: indent step still 16
    assert fake.expanded and fake.armed


def test_arm_prefers_the_search_box_and_clears_it_afterwards(tmp_path, monkeypatch) -> None:
    """Default path: the leaf name is typed into the Search box, and the leaf's
    OWN short menu is right-clicked (never the ancestor folder's, which can
    overrun the screen and clip the Execute entry). The box is cleared on the
    way out so the operator gets their tree back."""
    _fork_tree(tmp_path)
    fake = _FakeStudio(search_box=True)
    _wire(monkeypatch, fake)
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["ok"] and out["state"] == "armed", out
    assert out["expanded"] == ["filter:StudioMCPBridge"]
    assert [t["row"] for t in out["tried"]] == ["StudioMCPBridge"]
    assert out["tried"][0].get("leaf") is True
    assert "StudioMCPBridge" in fake.keys          # typed into the box
    assert fake.keys[-1] == "{Ctrl}a{Delete}"     # and cleared afterwards
    assert fake.filter == "" and not fake.expanded and fake.armed


def test_arm_names_the_ancestor_it_could_not_see(tmp_path, monkeypatch) -> None:
    """With the Project-view search filter active the whole tree is replaced by
    'No results found' -- report WHICH row was missing, not an empty tried."""
    _fork_tree(tmp_path)
    # The in-scene path label is the only identity left; it names the dir
    # execute_method was given.
    fake = _FakeStudio(path_label=str(tmp_path), show_root=False)
    fake._rows = lambda: [_Ctl(fake.path_label, "TextControl", (1084, 10, 499, 18)),
                          _Ctl('No results found for "x"', "TextControl", (966, 131, 300, 17))]
    fake.win.GetChildren = fake._rows
    _wire(monkeypatch, fake)
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["ok"] is False and out["error"] == "menu_item_not_found"
    assert out["tried"] == [{"row": "Misc.", "visible": False}]


def _menu_missing_fake(tmp_path, build: bool = True):
    """The 'No results' tree: menu step fails with menu_item_not_found."""
    if build:
        _fork_tree(tmp_path)
    fake = _FakeStudio(path_label=str(tmp_path), show_root=False)
    fake._rows = lambda: [_Ctl(fake.path_label, "TextControl", (1084, 10, 499, 18)),
                          _Ctl('No results found for "x"', "TextControl", (966, 131, 300, 17))]
    fake.win.GetChildren = fake._rows
    return fake


def test_arm_menu_failure_reports_os_dialog_when_foreign_window_in_front(
        tmp_path, monkeypatch) -> None:
    """Window enumeration mocked: a non-Studio foreground window turns the bare
    menu_item_not_found into probable_cause os_dialog + blocking_window + hint."""
    fake = _menu_missing_fake(tmp_path)
    _wire(monkeypatch, fake)
    monkeypatch.setattr(studio_arm, "_foreground_window",
                        lambda auto: {"title": "Windows Security Alert", "pid": 4242})
    monkeypatch.setattr(studio_arm, "_process_name", lambda pid: "wf.exe")
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["ok"] is False and out["error"] == "menu_item_not_found"
    assert out["probable_cause"] == "os_dialog"
    assert out["blocking_window"] == {"title": "Windows Security Alert",
                                      "process": "wf.exe", "pid": 4242,
                                      "class": "", "source": "uia"}
    assert "Windows Security Alert" in out["hint"] and "dismiss" in out["hint"]


def test_arm_menu_failure_unchanged_without_foreign_window(
        tmp_path, monkeypatch) -> None:
    """No foreground window, or Studio's own: the original error is untouched."""
    _fork_tree(tmp_path)
    for fg, name in ((None, None),
                     ({"title": "Cell_v5 - FactoryTalk Optix Studio", "pid": 77},
                      "FTOptixStudio.exe")):
        fake = _menu_missing_fake(tmp_path, build=False)
        _wire(monkeypatch, fake)
        monkeypatch.setattr(studio_arm, "_foreground_window", lambda auto, fg=fg: fg)
        monkeypatch.setattr(studio_arm, "_process_name", lambda pid, n=name: n)
        out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
        assert out["error"] == "menu_item_not_found"
        assert "probable_cause" not in out and "blocking_window" not in out
        assert "hint" not in out


def test_blocking_window_for_ignores_studio_pid_and_falls_back_to_pid() -> None:
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(studio_arm, "_foreground_window",
                       lambda auto: {"title": "Dlg", "pid": 9})
        monkey.setattr(studio_arm, "_process_name", lambda pid: None)
        monkey.setattr(studio_arm, "_win32_top_level_windows", lambda: [])
        assert studio_arm.blocking_window_for(None, 9) is None
        assert studio_arm.blocking_window_for(None, 1) == {
            "title": "Dlg", "process": "9", "pid": 9, "class": "", "source": "uia"}
    finally:
        monkey.undo()


_STUDIO_WIN = {"title": "Cell_v5 - FactoryTalk Optix Studio", "pid": 77,
               "rect": (0, 0, 1000, 800), "foreground": False}


def _win32_env(monkeypatch, windows) -> None:
    """No UIA foreground; Win32 enumeration returns `windows`."""
    monkeypatch.setattr(studio_arm, "_foreground_window", lambda auto: None)
    monkeypatch.setattr(studio_arm, "_win32_top_level_windows", lambda: windows)
    monkeypatch.setattr(studio_arm, "_process_name", lambda pid: {
        77: "FTOptixStudio.exe", 4242: "OneDrive.exe", 5: "notepad.exe"}.get(pid))


def test_win32_foreign_window_over_studio_is_os_dialog(tmp_path, monkeypatch) -> None:
    """A fake Win32 enumeration with a visible foreign window overlapping Studio
    gives probable_cause os_dialog and blocking_window.source == 'win32'."""
    _win32_env(monkeypatch, [
        {"title": "Sign in to OneDrive", "pid": 4242,
         "rect": (400, 300, 700, 500), "foreground": False},
        _STUDIO_WIN])
    out = studio_arm.annotate_os_dialog({"ok": False, "error": "menu_item_not_found"},
                                        None, 77)
    assert out["probable_cause"] == "os_dialog"
    assert out["blocking_window"] == {"title": "Sign in to OneDrive",
                                      "process": "OneDrive.exe", "pid": 4242,
                                      "class": "", "source": "win32"}
    assert "Sign in to OneDrive" in out["hint"] and "dismiss" in out["hint"]


def test_win32_foreground_foreign_window_counts_without_overlap(monkeypatch) -> None:
    _win32_env(monkeypatch, [
        {"title": "Far away", "pid": 5, "rect": (2000, 0, 2200, 100),
         "foreground": True}, _STUDIO_WIN])
    got = studio_arm.blocking_window_for(None, 77)
    assert got and got["source"] == "win32" and got["pid"] == 5


def test_win32_nothing_visible_leaves_behaviour_unchanged(monkeypatch) -> None:
    """No windows, only Studio's own, or foreign windows that neither overlap nor
    hold the foreground: no blocker and the failure dict is untouched."""
    for wins in ([], [_STUDIO_WIN],
                 [{"title": "Elsewhere", "pid": 5, "rect": (2000, 0, 2200, 100),
                   "foreground": False}, _STUDIO_WIN]):
        _win32_env(monkeypatch, wins)
        assert studio_arm.blocking_window_for(None, 77) is None
        base = {"ok": False, "error": "menu_item_not_found"}
        assert studio_arm.annotate_os_dialog(dict(base), None, 77) == base


def test_window_identity_accepts_browsename_alias_and_path_label() -> None:
    # Line4_HMI: root row reads Line4 (the project node), never Line4_HMI.
    fake = _FakeStudio(root_row="Line4")
    assert studio_arm._studio_window_for(fake, "Line4_HMI") is None
    assert studio_arm._studio_window_for(fake, "Line4_HMI", aliases={"line4"}) is fake.win
    # Filter active: root row gone, only the in-scene path label identifies it.
    fake2 = _FakeStudio(root_row="Line4", show_root=False,
                        path_label=r"C:\Users\me\Desktop\Line4_HMI\Line4.optix")
    assert studio_arm._studio_window_for(fake2, "Line4_HMI", aliases={"line4"}) is None
    assert studio_arm._studio_window_for(
        fake2, "Line4_HMI", aliases={"line4"},
        project_dir=r"C:\Users\me\Desktop\Line4_HMI") is fake2.win
    # A different project's label must not match by prefix.
    assert studio_arm._studio_window_for(
        fake2, "Line4_HM", project_dir=r"C:\Users\me\Desktop\Line4_HM") is None


def test_window_identity_tolerates_the_unsaved_star() -> None:
    r"""I34: Studio appends `*` to the project name/path while there are
    unsaved changes (`...\NavArgDemo*`), and every bridge-edited project is
    unsaved until Ctrl+S. Before the fix that answered studio_window_not_found,
    which reads exactly like 'Studio is not open'."""
    # Root row carries the marker.
    fake = _FakeStudio(root_row="NavArgDemo*")
    assert studio_arm._studio_window_for(fake, "NavArgDemo") is fake.win
    # Filter active: the in-scene PATH label is the only identity, and it
    # carries the marker too.
    fake2 = _FakeStudio(root_row="NavArgDemo", show_root=False,
                        path_label=r"C:\Users\op\Desktop\NavArgDemo*")
    assert studio_arm._studio_window_for(
        fake2, "NavArgDemo",
        project_dir=r"C:\Users\op\Desktop\NavArgDemo") is fake2.win
    # The star does not make a NON-matching name match.
    assert studio_arm._studio_window_for(fake, "OtherProj") is None


def test_served_names_include_optix_stem_and_root_node(cfg: core.Config) -> None:
    d = cfg.projects_root / "Line4_HMI"
    (d / "Nodes").mkdir(parents=True)
    (d / "Line4.optix").write_text("", encoding="utf-8")
    (d / "Nodes" / "Line4.yaml").write_text("Name: Line4\nType: Project\n", encoding="utf-8")
    names = core.project_served_names(d)
    assert names == {"line4_hmi", "line4"}
    assert core._bridge_name_match("Line4", names)
    assert core._bridge_name_match(" line4 ", names)
    assert not core._bridge_name_match("Other_v5", names)
    assert not core._bridge_name_match("", names)
    assert core._bridge_name_match("Line4_HMI", "line4_hmi")


def test_find_bridge_for_routes_by_browsename(cfg: core.Config, monkeypatch) -> None:
    """Every bridge tool goes through _find_bridge_for; before the alias it
    reported bridge_wrong_project for Line4_HMI while its bridge was up."""
    d = cfg.projects_root / "Line4_HMI"
    (d / "Nodes").mkdir(parents=True)
    (d / "Line4.optix").write_text("", encoding="utf-8")
    monkeypatch.setattr(core, "list_bridges", lambda c, force=False: [
        {"project": "Cell_v5", "port": 8768, "available": True},
        {"project": "Line4", "port": 8769, "available": True},
    ])
    assert core._find_bridge_for(cfg, "Line4_HMI")["port"] == 8769
    assert core._find_bridge_for(cfg, "Cell_v5")["port"] == 8768
    assert core._find_bridge_for(cfg, "Nobody") is None


# ---- acceptance tests: security-warning consent handling --------------------
#
# _tick_suppress toggles the "do not show this warning again" checkbox via the
# TogglePattern (with a click fallback). _click_consent walks all child controls
# of the popup BEFORE clicking anything, so the suppression tick always lands
# before the consent click — clicking Proceed first would destroy the dialog and
# leave the box unticked.


class _FakeTogglePattern:
    """Minimal stand-in for uiautomation's TogglePattern."""

    def __init__(self, state: int = 0) -> None:
        self.ToggleState = state   # 0 = off, 1 = on (ToggleState_On)
        self.toggled = False

    def Toggle(self) -> None:  # noqa: N802 — mirrors uiautomation
        self.toggled = True
        self.ToggleState = 1


class _FakeCheckbox(_Ctl):
    """CheckBoxControl that optionally exposes a TogglePattern."""

    def __init__(self, name: str, toggle_state: int = 0, has_toggle: bool = True,
                 rect: tuple = (200, 200, 150, 20)) -> None:
        super().__init__(name, "CheckBoxControl", rect)
        self._tp: _FakeTogglePattern | None = (
            _FakeTogglePattern(toggle_state) if has_toggle else None)

    def GetTogglePattern(self) -> _FakeTogglePattern:  # noqa: N802
        if self._tp is None:
            raise Exception("toggle pattern unavailable")
        return self._tp


class _SimpleAuto:
    """Minimal stand-in for the uiautomation module surface used by
    _tick_suppress and _click_consent."""

    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    def Click(self, x: int, y: int) -> None:  # noqa: N802
        self.clicks.append((x, y))


def _consent_win(
    button_name: str = "Proceed",
    with_suppress: bool = True,
    button_first: bool = False,
) -> tuple[_Ctl, _FakeCheckbox | None, _Ctl]:
    """Build a synthetic Studio window containing one in-scene popup.

    Returns (window, checkbox_or_None, button) so tests can inspect them.
    When *button_first* is True the ButtonControl appears before the
    CheckBoxControl in the popup's GetChildren() order — the implementation
    must still tick before clicking.
    """
    checkbox: _FakeCheckbox | None = None
    button = _Ctl(button_name, "ButtonControl", (220, 240, 80, 24))
    if with_suppress:
        checkbox = _FakeCheckbox("Do not show this warning again")
        children: list[_Ctl] = (
            [button, checkbox] if button_first else [checkbox, button])
    else:
        children = [button]
    popup = _Ctl("Security Warning", "PaneControl", (180, 180, 340, 120), children)
    win = _Ctl("FactoryTalk Optix Studio", "WindowControl", (0, 0, 1920, 1080), [popup])
    return win, checkbox, button


# -- _tick_suppress -----------------------------------------------------------

def test_tick_suppress_uses_toggle_pattern_when_off(monkeypatch) -> None:
    """TogglePattern.Toggle() is called when the checkbox is currently off."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    cb = _FakeCheckbox("Do not show this warning again", toggle_state=0)
    assert studio_arm._tick_suppress(auto, cb) is True
    assert cb._tp.toggled is True      # Toggle() fired
    assert auto.clicks == []            # click fallback NOT used


def test_tick_suppress_skips_toggle_when_already_on(monkeypatch) -> None:
    """When ToggleState is already 1 (on) no Toggle() call is made."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    cb = _FakeCheckbox("Do not show this warning again", toggle_state=1)
    assert studio_arm._tick_suppress(auto, cb) is True
    assert cb._tp.toggled is False     # already on — left alone
    assert auto.clicks == []


def test_tick_suppress_falls_back_to_click_when_pattern_unavailable(monkeypatch) -> None:
    """When GetTogglePattern() raises, _tick_suppress must click the control
    instead of propagating the exception."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    cb = _FakeCheckbox("Do not show this warning again", has_toggle=False)
    assert studio_arm._tick_suppress(auto, cb) is True
    r = cb.BoundingRectangle
    expected = (r.left + r.width() // 2, r.top + r.height() // 2)
    assert expected in auto.clicks


# -- _click_consent -----------------------------------------------------------

def test_click_consent_ticks_suppress_then_clicks_proceed(monkeypatch) -> None:
    """Baseline: the suppression checkbox is ticked and Proceed is clicked."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    win, checkbox, button = _consent_win("Proceed")
    assert studio_arm._click_consent(auto, win, attempts=1) is True
    assert checkbox._tp.toggled is True
    br = button.BoundingRectangle
    assert (br.left + br.width() // 2, br.top + br.height() // 2) in auto.clicks


def test_click_consent_ticks_before_clicking_even_when_button_appears_first(
        monkeypatch) -> None:
    """The whole popup is walked before anything is clicked. Even when the
    ButtonControl appears before the CheckBoxControl in traversal order, the
    toggle must fire before the Proceed click — clicking first destroys the
    dialog and leaves the box unticked."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    log: list[str] = []

    class _LoggedCheckbox(_FakeCheckbox):
        def GetTogglePattern(self) -> _FakeTogglePattern:
            tp = super().GetTogglePattern()
            orig = tp.Toggle
            def _log_toggle() -> None:
                log.append("toggled")
                orig()
            tp.Toggle = _log_toggle
            return tp

    class _LoggedAuto(_SimpleAuto):
        def Click(self, x: int, y: int) -> None:
            log.append(f"click:{x},{y}")
            super().Click(x, y)

    checkbox = _LoggedCheckbox("Do not show this warning again")
    button = _Ctl("Proceed", "ButtonControl", (220, 240, 80, 24))
    # Button intentionally listed BEFORE checkbox in popup children.
    popup = _Ctl("Security Warning", "PaneControl", (180, 180, 340, 120),
                 [button, checkbox])
    win = _Ctl("FactoryTalk Optix Studio", "WindowControl", (0, 0, 1920, 1080), [popup])
    auto = _LoggedAuto()
    assert studio_arm._click_consent(auto, win, attempts=1) is True
    assert "toggled" in log
    first_click_idx = next(i for i, e in enumerate(log) if e.startswith("click:"))
    assert log.index("toggled") < first_click_idx


def test_click_consent_works_without_suppress_checkbox(monkeypatch) -> None:
    """A popup with a consent button but no 'do not show again' checkbox still
    consents — the checkbox is best-effort and must not block the click."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    win, _cb, button = _consent_win("Proceed", with_suppress=False)
    assert studio_arm._click_consent(auto, win, attempts=1) is True
    br = button.BoundingRectangle
    assert (br.left + br.width() // 2, br.top + br.height() // 2) in auto.clicks


@pytest.mark.parametrize("label", ["Proceed", "Yes", "Run", "Execute", "OK"])
def test_click_consent_recognises_all_button_labels(monkeypatch, label: str) -> None:
    """_CONSENT covers every label Studio is known to use."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    win, _cb, _btn = _consent_win(label, with_suppress=False)
    assert studio_arm._click_consent(auto, win, attempts=1) is True


def test_click_consent_skips_optixstudio_named_panes(monkeypatch) -> None:
    """A pane whose name contains 'optixstudio' is the Studio chrome — it must
    never be treated as the security-warning popup."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    button = _Ctl("Proceed", "ButtonControl", (220, 240, 80, 24))
    studio_pane = _Ctl("FactoryTalk Optix Studio", "PaneControl",
                       (180, 180, 340, 120), [button])
    win = _Ctl("Studio", "WindowControl", (0, 0, 1920, 1080), [studio_pane])
    assert studio_arm._click_consent(auto, win, attempts=1) is False
    assert auto.clicks == []


def test_click_consent_returns_false_when_no_popup_present(monkeypatch) -> None:
    """Returns False — never raises — when no in-scene popup with a consent
    button is found after all retry attempts."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    # Window contains only plain tree rows, no popup.
    win = _Ctl("FactoryTalk Optix Studio", "WindowControl", (0, 0, 1920, 1080), [
        _Ctl("NetLogic", "TextControl", (100, 100, 200, 28)),
    ])
    assert studio_arm._click_consent(auto, win, attempts=2) is False


# ---- acceptance tests: optix_execute_method dispatch gate --------------------
#
# optix_execute_method (MCP tool) calls core.execute_design_method which calls
# studio_arm.execute_method(..., verify="none"). verify="none" means:
#   * no port-level guard (serving_port_for is never consulted)
#   * no "already_armed" / "not_running" short-circuit
#   * once the Execute menu item is clicked, state="executed" is returned
# The two tests below pin these two invariants off-box, without a live Studio.


def test_optix_execute_method_dispatch_path_returns_executed(
        tmp_path, monkeypatch) -> None:
    """UIA tree navigation succeeds and returns state='executed' for an
    arbitrary method when called with verify='none' (the optix_execute_method
    path). The fake Studio exposes 'Execute StopBridge' on the category
    folder's context menu; the gesture must navigate Misc. → expand →
    DesignTimeNetLogic → click that item — and then return 'executed' rather
    than waiting for a port signal (there is none for arbitrary methods)."""
    _fork_tree(tmp_path)
    fake = _FakeStudio()
    _wire(monkeypatch, fake)
    out = studio_arm.execute_method(
        "Cell_v5", str(tmp_path),
        method="StopBridge", node_name="StudioMCPBridge",
        verify="none",
    )
    assert out["ok"] and out["state"] == "executed", out
    assert "nudge" in out, "caller must be told to verify the effect separately"
    assert out["chain"] == ["Misc.", "DesignTimeNetLogic", "StudioMCPBridge"]
    # The gesture landed: DesignTimeNetLogic was expanded and its menu was used.
    assert fake.expanded and not fake.armed


def test_optix_execute_method_verify_none_skips_port_guard(
        tmp_path, monkeypatch) -> None:
    """With verify='none' serving_port_for is never called — there is no
    'already_armed' or 'not_running' fast-exit for arbitrary methods whose
    port-level effect is unknown. This is the key difference from bridge_arm."""
    _fork_tree(tmp_path)
    fake = _FakeStudio()
    port_guard_calls: list = []
    monkeypatch.setitem(__import__("sys").modules, "uiautomation", fake)
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    monkeypatch.setattr(studio_arm, "bound_ports", lambda *a, **k: set())
    monkeypatch.setattr(
        studio_arm, "serving_port_for",
        lambda *a, **k: port_guard_calls.append(a) or None,
    )
    out = studio_arm.execute_method(
        "Cell_v5", str(tmp_path),
        method="StopBridge", node_name="StudioMCPBridge",
        verify="none",
    )
    assert out["ok"] and out["state"] == "executed", out
    assert not port_guard_calls, (
        "serving_port_for must not be called when verify='none': "
        f"got {port_guard_calls}"
    )


# -- row shapes: 1.7 (label is a direct child) vs 1.8 (label in a group) -----

def _shape_pair():
    return _FakeStudio(), _FakeStudio(nested=True)


def test_row_texts_reads_direct_and_grouped_labels() -> None:
    flat, nested = _shape_pair()
    assert [c.Name for c in studio_arm._row_texts(flat.win)] == \
        [c.Name for c in studio_arm._row_texts(nested.win)]
    assert any(c.ControlTypeName == "ListItemControl" for c in nested.win.GetChildren())


def test_row_texts_is_depth_bounded() -> None:
    deep = _Ctl("t", "TextControl", (0, 0, 10, 10))
    for _ in range(3):
        deep = _Ctl("", "GroupControl", (0, 0, 10, 10), [deep])
    root = _Ctl("", "WindowControl", children=[deep])   # text at depth 4
    assert studio_arm._row_texts(root, max_depth=4) == [deep.GetChildren()[0]
                                                         .GetChildren()[0].GetChildren()[0]]
    assert studio_arm._row_texts(root, max_depth=3) == []


def test_row_texts_skips_menus() -> None:
    menu = _Ctl("", "MenuControl", children=[_Ctl("Misc.", "TextControl", (1, 1, 5, 5))])
    assert studio_arm._row_texts(_Ctl("", "WindowControl", children=[menu])) == []


def test_candidate_rows_pick_the_same_row_in_both_shapes() -> None:
    flat, nested = _shape_pair()
    flat.expanded = nested.expanded = True
    for names in (["Misc."], ["DesignTimeNetLogic"], ["studiomcpbridge"]):
        a = studio_arm._candidate_rows(flat.win, names)
        b = studio_arm._candidate_rows(nested.win, names)
        assert [(c.Name, c.BoundingRectangle.left, c.BoundingRectangle.top) for c in a] == \
            [(c.Name, c.BoundingRectangle.left, c.BoundingRectangle.top) for c in b]
        assert a
    # leftmost first: the tree's own copy before the Properties-pane copy
    rows = studio_arm._candidate_rows(nested.win, ["StudioMCPBridge"])
    assert rows[0].BoundingRectangle.left == 1666


def test_candidate_rows_strip_search_highlight_in_nested_shape() -> None:
    fake = _FakeStudio(search_box=True, nested=True)
    fake.filter = "StudioMCPBridge"
    rows = studio_arm._candidate_rows(fake.win, ["StudioMCPBridge"])
    assert [studio_arm._strip_tags(r.Name) for r in rows][0] == "StudioMCPBridge"


@pytest.mark.parametrize("nested", [False, True])
def test_indent_step_measures_both_shapes(nested: bool) -> None:
    fake = _FakeStudio(nested=nested)
    fake.expanded = True
    row = studio_arm._candidate_rows(fake.win, ["DesignTimeNetLogic"])[0]
    assert studio_arm._indent_step(fake.win, row) == 16


@pytest.mark.parametrize("nested", [False, True])
def test_click_chevron_works_in_both_shapes(nested: bool) -> None:
    fake = _FakeStudio(nested=nested)
    row = studio_arm._candidate_rows(fake.win, ["Misc."])[0]
    x = studio_arm._click_chevron(fake, fake.win, row)
    assert x == 1024 - 34 and fake.expanded


def test_arm_resolves_the_same_chain_in_both_shapes(tmp_path, monkeypatch) -> None:
    _fork_tree(tmp_path)
    results = []
    for nested in (False, True):
        for box in (False, True):
            fake = _FakeStudio(nested=nested, search_box=box)
            _wire(monkeypatch, fake)
            out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
            assert out["ok"] and out["state"] == "armed", out
            results.append((box, out["chain"], out["expanded"],
                            [t["row"] for t in out["tried"]]))
    assert results[0] == results[2] and results[1] == results[3]


# --- keyboard-focus gating for editing keys (ftx-mcp-we6.2) -----------------

class _FocusRect:
    left, top = 10, 10

    def width(self) -> int:
        return 100

    def height(self) -> int:
        return 20


class _FakeAuto:
    """Records SendKeys / Click; GetFocusedControl returns what the test sets."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.clicks = 0
        self.focused = None

    def Click(self, x, y) -> None:
        self.clicks += 1

    def SendKeys(self, keys) -> None:
        self.keys.append(keys)

    def GetFocusedControl(self):
        return self.focused


class _FakeBox:
    """Gains keyboard focus on the Nth click (None = never)."""

    BoundingRectangle = _FocusRect()

    def __init__(self, auto: _FakeAuto, focus_on_attempt: int | None,
                 value_pattern: bool = False) -> None:
        self.auto = auto
        self.focus_on_attempt = focus_on_attempt
        self.HasKeyboardFocus = False
        self.value: str | None = None
        self._vp = value_pattern

    def SetFocus(self) -> None:
        if self.focus_on_attempt is not None and self.auto.clicks >= self.focus_on_attempt:
            self.HasKeyboardFocus = True

    def GetValuePattern(self):
        if not self._vp:
            raise RuntimeError("ValuePattern not supported")
        box = self

        class _VP:
            def SetValue(self, v) -> None:
                box.value = v

        return _VP()


def test_never_focused_box_receives_no_keystrokes() -> None:
    auto = _FakeAuto()
    box = _FakeBox(auto, None)
    with pytest.raises(studio_arm.FocusError, match="refusing to send keys blind"):
        studio_arm._set_filter(auto, box, "Leaf", settle=0)
    assert auto.keys == []


def test_box_focused_on_second_attempt_receives_keys_exactly_once() -> None:
    auto = _FakeAuto()
    box = _FakeBox(auto, 2)
    studio_arm._set_filter(auto, box, "Leaf", settle=0)
    assert auto.clicks == 2
    assert auto.keys == ["{Ctrl}a{Delete}", "Leaf"]


def test_value_pattern_sets_value_without_keystrokes() -> None:
    auto = _FakeAuto()
    box = _FakeBox(auto, None, value_pattern=True)
    studio_arm._set_filter(auto, box, "Leaf", settle=0)
    assert box.value == "Leaf"
    assert auto.keys == []


def test_no_focus_and_no_value_pattern_raises_clear_error() -> None:
    auto = _FakeAuto()
    box = _FakeBox(auto, None, value_pattern=False)
    with pytest.raises(studio_arm.FocusError, match="no ValuePattern"):
        studio_arm._set_filter(auto, box, "", settle=0)
    assert auto.keys == []


def test_focused_control_equal_to_box_counts_as_focus() -> None:
    auto = _FakeAuto()
    box = _FakeBox(auto, None)
    auto.focused = box
    studio_arm._set_filter(auto, box, "", settle=0)
    assert auto.keys == ["{Ctrl}a{Delete}"]


def test_focus_lost_before_typing_sends_no_text() -> None:
    auto = _FakeAuto()
    box = _FakeBox(auto, 1)

    def drop_focus(keys) -> None:
        auto.keys.append(keys)
        box.HasKeyboardFocus = False

    auto.SendKeys = drop_focus  # type: ignore[method-assign]
    with pytest.raises(studio_arm.FocusError):
        studio_arm._set_filter(auto, box, "Leaf", settle=0.001)
    assert auto.keys == ["{Ctrl}a{Delete}"]


def test_every_editing_sendkeys_in_studio_arm_is_gated() -> None:
    """Only the focus-gated _set_filter may send Ctrl+A/Delete/typed text."""
    import inspect
    import re as _re

    src = inspect.getsource(studio_arm)
    calls = _re.findall(r"auto\.SendKeys\(([^)]*)\)", src)
    assert sorted(calls) == sorted(['"{Ctrl}a{Delete}"', "text", '"{Esc}"', '"{Esc}"', '"{Esc}"'])
    body = inspect.getsource(studio_arm._set_filter)
    assert body.count("SendKeys(") == 2


# ---- clear_consent project-directory resolution ------------------------------

def _stub_uia(monkeypatch, seen: dict) -> None:
    """Fake `uiautomation` and capture the args _studio_window_for receives."""
    import sys
    import types

    mod = types.ModuleType("uiautomation")
    monkeypatch.setitem(sys.modules, "uiautomation", mod)

    def fake_window_for(auto, project, aliases=None, project_dir=None):
        seen.update(project=project, aliases=aliases, project_dir=project_dir)
        return None  # -> studio_window_not_found, after resolution

    monkeypatch.setattr(studio_arm, "_studio_window_for", fake_window_for)


def test_clear_consent_explicit_project_dir_is_used(monkeypatch) -> None:
    seen: dict = {}
    _stub_uia(monkeypatch, seen)

    def boom(*a, **k):
        raise AssertionError("resolve_project must not be called")

    monkeypatch.setattr(core, "resolve_project", boom)
    out = studio_arm.clear_consent("Line4", project_dir="C:/p/Line4")
    assert out["error"] == "studio_window_not_found"
    assert seen["project_dir"] == "C:/p/Line4"


def test_clear_consent_name_only_resolves_via_resolve_project(
        monkeypatch, tmp_path) -> None:
    seen: dict = {}
    _stub_uia(monkeypatch, seen)
    cfg = object()
    calls = []

    def fake_resolve(c, project):
        calls.append((c, project))
        return tmp_path

    monkeypatch.setattr(core, "resolve_project", fake_resolve)
    out = studio_arm.clear_consent("Line4", cfg=cfg)
    assert calls == [(cfg, "Line4")]
    assert seen["project_dir"] == tmp_path
    assert out["error"] == "studio_window_not_found"


def test_clear_consent_unresolvable_project_returns_structured_error(
        monkeypatch) -> None:
    seen: dict = {}
    _stub_uia(monkeypatch, seen)

    def fake_resolve(c, project):
        raise core.ProjectNotFound(f"project not found: {project}")

    monkeypatch.setattr(core, "resolve_project", fake_resolve)
    out = studio_arm.clear_consent("Ghost", cfg=object())
    assert out["ok"] is False
    assert out["error"] == "project_not_found"
    assert out["project"] == "Ghost"
    assert "Ghost" in out["detail"]
    assert seen == {}  # never reached the window lookup


def _fake_uia(monkeypatch, top_level) -> None:
    """Install a fake `uiautomation` whose desktop root lists `top_level`."""
    import sys
    import types

    class Ctrl:
        def __init__(self, ctype, name, children=None):
            self.ControlTypeName, self.Name = ctype, name
            self.ProcessId = 1
            self._c = children or []

        def GetChildren(self):
            return self._c

    def build(spec):
        ctype, name, kids = spec
        return Ctrl(ctype, name, [build(k) for k in kids])

    root = Ctrl("PaneControl", "Desktop", [build(s) for s in top_level])
    mod = types.ModuleType("uiautomation")
    mod.GetRootControl = lambda: root  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uiautomation", mod)


def _project_with_optix(cfg: core.Config, name: str) -> None:
    d = cfg.projects_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.optix").write_text("Name: " + name, encoding="utf-8")
    (d / "IDEVersion.txt").write_text("1.8.0.555", encoding="utf-8")


def test_project_open_returns_upgrade_required_on_upgrade_modal(
        cfg: core.Config, monkeypatch) -> None:
    import time
    _project_with_optix(cfg, "OldProj")
    # A real project file is non-empty; an empty one is project_file_missing.
    (cfg.projects_root / "OldProj" / "OldProj.optix").write_text("Name: OldProj")
    (cfg.projects_root / "OldProj" / "IDEVersion.txt").write_text("1.7.4.32")
    monkeypatch.setattr(core, "_studio_launch", lambda *a: {"ok": True, "pid": 1})
    monkeypatch.setattr(core, "_studio_log_since", lambda mark, **k: [
        "ERROR The project was created with an older version of FactoryTalk Optix Studio"])
    monkeypatch.setattr(core, "studio_version",
                        lambda *a, **k: {"ok": True, "stdout": "1.8.0.555"})
    clicked: list[str] = []
    monkeypatch.setattr(studio_arm, "_studio_window_for", lambda *a, **k: None)
    _fake_uia(monkeypatch, [
        ("WindowControl", "FactoryTalk Optix Studio", [
            ("WindowControl", "Project Upgrade", [
                ("TextControl", "This project was created with an older version. Upgrade it?", []),
                ("ButtonControl", "Upgrade", []),
            ]),
        ]),
    ])
    monkeypatch.setattr(time, "sleep", lambda s: clicked.append("slept"))
    t0 = time.time()
    out = core.project_open(cfg, "OldProj", wait_seconds=90)
    assert time.time() - t0 < 5
    assert out["ok"] is False and out["error"] == "upgrade_required"
    assert out["project"] == "OldProj"
    assert out["studio_version"] == "1.8.0.555"
    assert out["hint"]
    assert clicked == []  # returned on the first poll, never waited, never clicked


def test_project_open_still_times_out_without_upgrade_modal(
        cfg: core.Config, monkeypatch) -> None:
    _project_with_optix(cfg, "Plain")
    monkeypatch.setattr(core, "_studio_launch", lambda *a: {"ok": True, "pid": 1})
    monkeypatch.setattr(studio_arm, "_studio_window_for", lambda *a, **k: None)
    _fake_uia(monkeypatch, [("WindowControl", "FactoryTalk Optix Studio", [])])
    out = core.project_open(cfg, "Plain", wait_seconds=0.2)
    assert out["ok"] is False and out["error"] == "open_timeout"


# ---- acceptance tests: consent press by UIA InvokePattern + dismissal -------

class _FakeInvoke:
    def __init__(self, on_invoke=None) -> None:
        self.invoked = 0
        self._on_invoke = on_invoke

    def Invoke(self) -> None:  # noqa: N802 -- mirrors uiautomation
        self.invoked += 1
        if self._on_invoke:
            self._on_invoke()


class _InvokableButton(_Ctl):
    """ButtonControl exposing an InvokePattern (or not)."""

    def __init__(self, name, rect, has_invoke=True, on_invoke=None) -> None:
        super().__init__(name, "ButtonControl", rect)
        self._ip = _FakeInvoke(on_invoke) if has_invoke else None

    def GetInvokePattern(self):  # noqa: N802
        if self._ip is None:
            raise Exception("invoke pattern unavailable")
        return self._ip


def _popup_win(button, checkbox=None, dismiss_on_press=True):
    kids = [checkbox, button] if checkbox is not None else [button]
    popup = _Ctl("Security Warning", "PaneControl", (180, 180, 340, 400), kids)
    win = _Ctl("FactoryTalk Optix Studio", "WindowControl", (0, 0, 800, 600), [popup])
    if dismiss_on_press:
        def _gone() -> None:
            win._children.remove(popup)
        return win, _gone
    return win, None


def test_consent_below_work_area_is_invoked_not_clicked(monkeypatch) -> None:
    """Proceed centre at y=559, below the 800x600 work area (ends y=552):
    InvokePattern presses it and no coordinate click is made."""
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    cb = _FakeCheckbox("Do not show this warning again")
    holder: dict = {}
    btn = _InvokableButton("Proceed", (360, 547, 80, 24),
                           on_invoke=lambda: holder["gone"]())
    win, holder["gone"] = _popup_win(btn, cb)
    rep: dict = {}
    assert studio_arm._click_consent(auto, win, attempts=1, report=rep) is True
    assert btn._ip.invoked == 1
    assert auto.clicks == []
    assert cb._tp.toggled is True
    assert rep["consent_dismissed"] is True


def test_consent_button_without_invoke_pattern_falls_back_to_click(monkeypatch) -> None:
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    holder: dict = {}
    btn = _InvokableButton("Proceed", (220, 240, 80, 24), has_invoke=False)
    win, holder["gone"] = _popup_win(btn)
    orig_click = auto.Click
    auto.Click = lambda x, y: (orig_click(x, y), holder["gone"]())  # click dismisses
    rep: dict = {}
    assert studio_arm._click_consent(auto, win, attempts=1, report=rep) is True
    assert auto.clicks == [(260, 252)]
    assert rep["consent_dismissed"] is True


def test_consent_ticks_toggle_before_invoking_proceed(monkeypatch) -> None:
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    log: list[str] = []
    cb = _FakeCheckbox("Do not show this warning again")
    orig = cb._tp.Toggle
    cb._tp.Toggle = lambda: (log.append("toggle"), orig())
    btn = _InvokableButton("Proceed", (220, 240, 80, 24),
                           on_invoke=lambda: log.append("invoke"))
    win, _ = _popup_win(btn, cb, dismiss_on_press=False)
    studio_arm._click_consent(_SimpleAuto(), win, attempts=1)
    assert log == ["toggle", "invoke"]


def test_consent_surviving_dialog_reports_not_dismissed(monkeypatch) -> None:
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    btn = _InvokableButton("Proceed", (220, 240, 80, 24))
    win, _ = _popup_win(btn, dismiss_on_press=False)
    rep: dict = {}
    assert studio_arm._click_consent(_SimpleAuto(), win, attempts=1, report=rep) is True
    assert rep["consent_dismissed"] is False
    assert rep["dialog_title"] == "Security Warning"


def test_clear_consent_reports_dismissed_alongside_clicked(monkeypatch) -> None:
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    auto = _SimpleAuto()
    auto.GetCursorPos = lambda: (0, 0)
    auto.GetForegroundControl = lambda: None
    auto.SetCursorPos = lambda x, y: None
    monkeypatch.setitem(__import__("sys").modules, "uiautomation", auto)
    btn = _InvokableButton("Proceed", (220, 240, 80, 24))
    win, _ = _popup_win(btn, dismiss_on_press=False)
    monkeypatch.setattr(studio_arm, "_studio_window_for", lambda *a, **k: win)
    out = studio_arm.clear_consent("P", project_dir="x", attempts=1)
    assert out["consent_clicked"] is True and out["consent_dismissed"] is False
    assert out["error"] == "consent_not_dismissed" and out["dialog"] == "Security Warning"


def test_arm_returns_consent_not_dismissed_with_dialog_title(tmp_path, monkeypatch) -> None:
    """A dialog that survives the press fails the arm as consent_not_dismissed
    (with the dialog title), not as a later verify_timeout."""
    _fork_tree(tmp_path)
    fake = _FakeStudio()
    _wire(monkeypatch, fake)
    monkeypatch.setattr(
        studio_arm, "_click_consent",
        lambda auto, win, *a, report=None, **k: (
            report.update(consent_dismissed=False, dialog_title="Security Warning"),
            True)[1])
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge",
                                    timeout=0.2)
    assert out["ok"] is False and out["error"] == "consent_not_dismissed", out
    assert out["dialog"] == "Security Warning"
    assert out["consent_clicked"] is True and out["consent_dismissed"] is False


# ---- acceptance tests: expansion of collapsed ancestors (nested bridge) -----

class _ExpandPattern:
    def __init__(self, tree: _TreeStudio, i: int) -> None:
        self._tree, self._i = tree, i

    @property
    def ExpandCollapseState(self) -> int:  # noqa: N802 -- mirrors uiautomation
        return 1 if self._tree.expanded[self._i] else 0

    def Expand(self) -> None:  # noqa: N802
        self._tree.pattern_calls.append(self._i)
        self._tree.expanded[self._i] = True


class _TreeRow(_Ctl):
    """Row label that may expose an ExpandCollapsePattern."""

    def __init__(self, tree, i, name, rect) -> None:
        super().__init__(name, "TextControl", rect)
        self._tree, self._i = tree, i

    def GetExpandCollapsePattern(self):  # noqa: N802
        if not self._tree.has_pattern:
            raise Exception("expand/collapse pattern unavailable")
        return _ExpandPattern(self._tree, self._i)


class _TreeStudio:
    """Synthetic filtered Project tree for the 4-deep chain
    NetLogic > Misc. > DesignTimeNetLogic > StudioMCPBridge (the NESTED yaml).

    Row i is shown only while ancestors 0..i-1 are all expanded, and every
    ancestor starts COLLAPSED. Expansion works through ExpandCollapsePattern
    (`has_pattern`) and/or a click 34 px left of the row text (`has_chevron`).
    `leaf_pinned` shows the filtered leaf row even with its ancestors closed;
    `leaf_menu_needs_expand` gives it an empty menu until the chain is open;
    `security_gate` makes Execute raise a top-level NetLogic security warning
    that must be answered (Proceed, by InvokePattern) before the bridge serves.
    """
    NAMES = ["NetLogic", "Misc.", "DesignTimeNetLogic", "StudioMCPBridge"]

    def __init__(self, has_pattern=True, has_chevron=True, leaf_pinned=False,
                 leaf_menu_needs_expand=False, security_gate=False) -> None:
        self.has_pattern, self.has_chevron = has_pattern, has_chevron
        self.leaf_pinned = leaf_pinned
        self.leaf_menu_needs_expand = leaf_menu_needs_expand
        self.security_gate = security_gate
        self.expanded = [False, False, False]
        self.pattern_calls: list[int] = []
        self.chevron_clicks: list[int] = []
        self.right_clicks: list[str] = []
        self.menu = None
        self.armed = False
        self.focus = None
        self.filter = ""
        self.keys: list[str] = []
        self.foreign_pressed = 0
        self.tops: list = []
        self.win = _Ctl("FactoryTalk Optix Studio", "WindowControl", (961, 0, 958, 1031))
        self.win.GetChildren = self._rows

    def _shown(self, i: int) -> bool:
        return all(self.expanded[:i]) or (i == len(self.NAMES) - 1 and self.leaf_pinned)

    def _rect(self, i: int) -> tuple:
        return (1008 + 16 * i, 126 + 28 * i, 297 - 16 * i, 28)

    def _rows(self):
        rows = [_FocusableEdit(self, (961, 99, 349, 27)),
                _Ctl("Cell_v5", "TextControl", (1008, 98, 297, 20))]
        for i, n in enumerate(self.NAMES):
            if not self._shown(i):
                continue
            label = f"<b>{n}</b>" if i == len(self.NAMES) - 1 else n
            rows.append(_TreeRow(self, i, label, self._rect(i)))
        rows.append(_Ctl("StudioMCPBridge", "TextControl", (1666, 99, 243, 28)))
        if self.menu is not None:
            rows.append(self.menu)
        return rows

    def _row_at(self, x, y):
        for c in self._rows():
            r = c.BoundingRectangle
            if c.ControlTypeName == "TextControl" and r.left <= x < r.left + r.width() \
                    and r.top <= y < r.top + r.height():
                return studio_arm._strip_tags(c.Name)
        return None

    def _menu(self, names):
        items = [_Ctl(n, "MenuItemControl", (1100, 300 + 22 * i, 220, 22))
                 for i, n in enumerate(names)]
        return _Ctl("", "MenuControl", (1100, 300, 220, 22 * len(names)), items)

    # --- uiautomation module surface
    def GetRootControl(self):  # noqa: N802
        return _Ctl("", "PaneControl", children=[self.win, *self.tops])

    def GetCursorPos(self):  # noqa: N802
        return (0, 0)

    def SetCursorPos(self, x, y):  # noqa: N802
        pass

    def GetForegroundControl(self):  # noqa: N802
        return None

    def RightClick(self, x, y):  # noqa: N802
        row = self._row_at(x, y)
        self.right_clicks.append(str(row))
        if row == self.NAMES[-1]:
            if self.leaf_menu_needs_expand and not all(self.expanded):
                self.menu = self._menu([])
            else:
                self.menu = self._menu(["NodeSet", "Rename", "Execute StopBridge",
                                        "Execute StartBridge"])
        elif row in self.NAMES:
            self.menu = self._menu(["NodeSet", "Rename", "Delete", "New"])
        else:
            self.menu = None

    def _raise_security_warning(self) -> None:
        def _proceed():
            self.armed = True
            self.tops.remove(dlg)
        btn = _InvokableButton("Proceed", (400, 500, 80, 24), on_invoke=_proceed)
        dlg = _Ctl("NetLogic Security Warning", "WindowControl", (300, 300, 400, 260), [btn])
        dlg.ProcessId = self.win.ProcessId
        # Another application's look-alike dialog: must never be touched.
        other_btn = _InvokableButton(
            "Proceed", (400, 500, 80, 24),
            on_invoke=lambda: setattr(self, "foreign_pressed", self.foreign_pressed + 1))
        other = _Ctl("Security Warning", "WindowControl", (0, 0, 400, 260), [other_btn])
        other.ProcessId = self.win.ProcessId + 1
        self.tops.extend([other, dlg])

    def Click(self, x, y):  # noqa: N802
        self.focus = "search" if 99 <= y < 126 and x < 1310 else "tree"
        if self.menu is not None:
            for it in self.menu.GetChildren():
                r = it.BoundingRectangle
                if r.left <= x < r.left + r.width() and r.top <= y < r.top + r.height():
                    if it.Name == "Execute StartBridge":
                        if self.security_gate:
                            self._raise_security_warning()
                        else:
                            self.armed = True
                    self.menu = None
                    return
        if not self.has_chevron:
            return
        for i in range(len(self.expanded)):
            if self._shown(i):
                r = self._rect(i)
                if abs(x - (r[0] - 34)) <= 4 and r[1] <= y < r[1] + r[3]:
                    self.chevron_clicks.append(i)
                    self.expanded[i] = not self.expanded[i]

    def SendKeys(self, keys):  # noqa: N802
        self.keys.append(keys)
        if "{Esc}" in keys:
            self.menu = None
        elif self.focus == "search":
            self.filter = "" if keys == "{Ctrl}a{Delete}" else (
                self.filter if keys.startswith("{") else keys)


def _tree_project(tmp_path: Path) -> Path:
    nodes = tmp_path / "Nodes" / "NetLogic"
    nodes.mkdir(parents=True)
    (nodes / "NetLogic.yaml").write_text(NESTED, encoding="utf-8")
    (tmp_path / "Cell_v5.optix").write_text("", encoding="utf-8")
    return tmp_path


def _wire_tree(monkeypatch, fake: _TreeStudio) -> None:
    monkeypatch.setitem(__import__("sys").modules, "uiautomation", fake)
    monkeypatch.setattr(studio_arm.time, "sleep", lambda s: None)
    monkeypatch.setattr(studio_arm, "bound_ports", lambda *a, **k: set())
    monkeypatch.setattr(studio_arm, "serving_port_for",
                        lambda *a, **k: 8768 if fake.armed else None)


def test_four_deep_chain_with_collapsed_ancestors_resolves_leaf_and_menu(
        tmp_path, monkeypatch) -> None:
    """Every ancestor collapsed: each is expanded in order through
    ExpandCollapsePattern (no chevron click), then the LEAF row's own context
    menu supplies Execute StartBridge."""
    _tree_project(tmp_path)
    fake = _TreeStudio()
    _wire_tree(monkeypatch, fake)
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["ok"] and out["state"] == "armed", out
    assert out["chain"] == _TreeStudio.NAMES
    assert fake.pattern_calls == [0, 1, 2]          # in order, outermost first
    assert fake.chevron_clicks == []
    assert out["expanded"] == ["filter:StudioMCPBridge", "pattern:NetLogic",
                               "pattern:Misc.", "pattern:DesignTimeNetLogic"]
    assert fake.right_clicks == ["StudioMCPBridge"]  # only the leaf, never a folder
    assert out["tried"][-1]["leaf"] is True and out["tried"][-1]["menu_items"] == 4
    assert fake.filter == ""                         # operator's tree handed back


def test_collapsed_ancestors_fall_back_to_chevron_click(tmp_path, monkeypatch) -> None:
    _tree_project(tmp_path)
    fake = _TreeStudio(has_pattern=False)
    _wire_tree(monkeypatch, fake)
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["ok"] and out["state"] == "armed", out
    assert fake.pattern_calls == []
    assert fake.chevron_clicks == [0, 1, 2]
    assert out["expanded"][1:] == ["chevron:NetLogic", "chevron:Misc.",
                                   "chevron:DesignTimeNetLogic"]


def test_leaf_with_empty_menu_triggers_expansion_and_retry(tmp_path, monkeypatch) -> None:
    """Ancestors all on screen but collapsed-state leaf shows 0 menu items:
    expand, then retry the leaf."""
    _tree_project(tmp_path)
    fake = _TreeStudio(leaf_pinned=True, leaf_menu_needs_expand=True)
    fake.expanded = [True, True, False]    # chain visible; 'DesignTimeNetLogic' closed
    _wire_tree(monkeypatch, fake)
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["ok"] and out["state"] == "armed", out
    leaf_tries = [t for t in out["tried"] if t.get("leaf")]
    counts = [t["menu_items"] for t in leaf_tries]
    assert counts[0] == 0 and counts[-1] == 4   # empty menu, then retry after expanding
    assert fake.pattern_calls == [2]


def test_leaf_only_fallback_when_expansion_is_impossible(tmp_path, monkeypatch) -> None:
    """No pattern, chevron inert: the filtered leaf row is right-clicked
    directly and its menu still arms."""
    _tree_project(tmp_path)
    fake = _TreeStudio(has_pattern=False, has_chevron=False, leaf_pinned=True)
    _wire_tree(monkeypatch, fake)
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["ok"] and out["state"] == "armed", out
    assert fake.right_clicks == ["StudioMCPBridge"]
    leaf = [t for t in out["tried"] if t.get("leaf")]
    assert len(leaf) == 1 and leaf[0]["direct"] is True
    assert fake.expanded == [False, False, False]
    assert not any(e.startswith(("pattern:", "chevron:")) for e in out["expanded"])


def test_verify_timeout_presses_proceed_on_studio_owned_security_window(
        tmp_path, monkeypatch) -> None:
    """Execute raises a top-level NetLogic security-warning window (not an
    in-scene popup). On verify timeout the one owned by the Studio pid is
    answered through the UIA invoke helper; a same-titled foreign-pid dialog
    is left alone."""
    _tree_project(tmp_path)
    fake = _TreeStudio(security_gate=True)
    _wire_tree(monkeypatch, fake)
    invoked: list[object] = []
    orig_press = studio_arm._press
    monkeypatch.setattr(studio_arm, "_press",
                        lambda auto, b: (invoked.append(b), orig_press(auto, b))[1])
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge",
                                    timeout=0.05)
    assert out["ok"] and out["state"] == "armed", out
    assert out["security_warning_cleared"] is True
    assert out["security_warning"] == {"title": "NetLogic Security Warning",
                                       "method": "invoke", "dismissed": True}
    assert len(invoked) == 1 and fake.foreign_pressed == 0


def test_verify_timeout_without_security_window_still_times_out(
        tmp_path, monkeypatch) -> None:
    _tree_project(tmp_path)
    fake = _TreeStudio()
    _wire_tree(monkeypatch, fake)
    monkeypatch.setattr(studio_arm, "serving_port_for", lambda *a, **k: None)
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge",
                                    timeout=0.05)
    assert out["ok"] is False and out["error"] == "verify_timeout"
    assert "security_warning" not in out


def test_execute_method_uses_live_chain_without_yaml(tmp_path, monkeypatch) -> None:
    """A live chain drives the gesture even when no YAML declares the node."""
    (tmp_path / "Cell_v5.optix").write_text("", encoding="utf-8")
    fake = _FakeStudio()
    _wire(monkeypatch, fake)
    out = studio_arm.execute_method(
        "Cell_v5", str(tmp_path), method="StopBridge", node_name="StudioMCPBridge",
        verify="none", live_chain=["Misc.", "DesignTimeNetLogic", "StudioMCPBridge"])
    assert out["ok"] and out["state"] == "executed", out
    assert out["chain"] == ["Misc.", "DesignTimeNetLogic", "StudioMCPBridge"]
    assert out["chain_source"] == "bridge"


def test_execute_method_without_live_chain_uses_yaml(tmp_path, monkeypatch) -> None:
    _fork_tree(tmp_path)
    _wire(monkeypatch, _FakeStudio())
    out = studio_arm.execute_method(
        "Cell_v5", str(tmp_path), method="StopBridge", node_name="StudioMCPBridge",
        verify="none")
    assert out["chain_source"] == "yaml"
    assert out["chain"] == ["Misc.", "DesignTimeNetLogic", "StudioMCPBridge"]


# ---- #24: elevated Studio vs non-elevated service ---------------------------

def test_studio_elevated_service_not_decision() -> None:
    probe = {"service_elevated": False,
             "studio": [{"pid": 4242, "elevated": True, "denied": True}]}
    out = studio_arm.window_missing_result("Line4", probe)
    assert out["ok"] is False
    assert out["error"] == "studio_elevated_service_not"
    assert out["hint"] == ("Studio is running as administrator; start it "
                           "without elevation, or run the service elevated")
    assert out["studio_pids"] == [4242]


def test_studio_window_not_found_without_elevation_mismatch() -> None:
    for probe in (
        None,
        {"service_elevated": False, "studio": []},
        {"service_elevated": False,
         "studio": [{"pid": 1, "elevated": False, "denied": False}]},
        {"service_elevated": True,
         "studio": [{"pid": 1, "elevated": True, "denied": False}]},
    ):
        out = studio_arm.window_missing_result("Line4", probe)
        assert out["error"] == "studio_window_not_found"
        assert out["project"] == "Line4"


def test_execute_method_uses_elevation_decision(monkeypatch) -> None:
    import sys
    import types
    monkeypatch.setitem(sys.modules, "uiautomation", types.ModuleType("uiautomation"))
    monkeypatch.setattr(studio_arm, "_studio_window_for", lambda *a, **k: None)
    monkeypatch.setattr(studio_arm, "probe_elevation", lambda *a, **k: {
        "service_elevated": False,
        "studio": [{"pid": 7, "elevated": True, "denied": True}]})
    out = studio_arm.execute_method("Line4", "C:/p/Line4", verify="none",
                                    live_chain=["StudioMCPBridge"])
    assert out["error"] == "studio_elevated_service_not"


def test_elevation_powershell_helper_reports_elevation() -> None:
    ps = studio_arm._ELEVATION_PS
    assert "OpenProcessToken" in ps and "GetTokenInformation" in ps
    assert "elevated" in ps and "service_elevated" in ps
    assert "FTOptixStudio" in ps


def test_probe_elevation_parses_single_studio_object() -> None:
    class P:
        returncode = 0
        stdout = '{"service_elevated":false,"studio":{"pid":9,"elevated":true,"denied":true}}'

    class R:
        def run_powershell(self, ps, timeout, **kw):
            return P()

    out = studio_arm.probe_elevation(R())
    assert out == {"service_elevated": False,
                   "studio": [{"pid": 9, "elevated": True, "denied": True}]}


def test_cmdline_project_paths_accepts_new_name_root(tmp_path: Path) -> None:
    root = str(tmp_path)
    got = studio_arm.cmdline_project_paths(["C:\\Studio\\FTOptixStudio.exe", "new", "X", root, "--silent"])
    assert len(got) == 2 and got[0].replace("\\", "/").endswith("/X")
    assert studio_arm.cmdline_project_paths(["FTOptixStudio.exe", "open", "rel/Y.optix"]) == []


def test_title_name_only_ok_false_requires_the_dir() -> None:
    f = studio_arm.window_title_names_project
    assert f("Demo - FactoryTalk Optix Studio", "Demo")
    assert not f("Demo - FactoryTalk Optix Studio", "Demo", name_only_ok=False)
    assert f("C:\\p\\Demo - FactoryTalk Optix Studio", "Demo", None, "C:\\p\\Demo", name_only_ok=False)


def test_blocking_window_for_never_returns_own_process_tree(monkeypatch) -> None:
    """The service pid, its parent and the hosting conhost/WindowsTerminal are
    never a blocker, on either the UIA or the Win32 source; a real foreign
    window still is."""
    own = {900, 901, 902, 903}      # service, parent shell, conhost, WindowsTerminal
    calls = []
    monkeypatch.setattr(studio_arm, "_own_process_tree_pids",
                        lambda: calls.append(1) or set(own))
    names = {900: "python.exe", 901: "powershell.exe", 902: "conhost.exe",
             903: "WindowsTerminal.exe", 77: "FTOptixStudio.exe", 5: "notepad.exe"}
    monkeypatch.setattr(studio_arm, "_process_name", lambda pid: names.get(pid))
    own_wins = [{"title": f"own{p}", "pid": p, "rect": (0, 0, 2000, 2000),
                 "foreground": p == 903} for p in sorted(own)]
    monkeypatch.setattr(studio_arm, "_win32_top_level_windows",
                        lambda: own_wins + [_STUDIO_WIN])
    for pid in sorted(own):                      # UIA foreground in our tree
        monkeypatch.setattr(studio_arm, "_foreground_window",
                            lambda auto, pid=pid: {"title": "console", "pid": pid})
        calls.clear()
        assert studio_arm.blocking_window_for(object(), 77) is None
        assert len(calls) == 1                   # exclusion set computed once
    monkeypatch.setattr(studio_arm, "_win32_top_level_windows", lambda: own_wins + [
        _STUDIO_WIN, {"title": "Real dialog", "pid": 5, "rect": (10, 10, 50, 50),
                      "foreground": False}])
    monkeypatch.setattr(studio_arm, "_foreground_window", lambda auto: None)
    got = studio_arm.blocking_window_for(None, 77)
    assert got and got["pid"] == 5 and got["source"] == "win32"


def test_own_process_tree_pids_includes_self_and_parent() -> None:
    import os
    pids = studio_arm._own_process_tree_pids()
    assert os.getpid() in pids and os.getppid() in pids


# ---- PRD 0051 (#67): blocking-window attribution ----------------------------

def _hint_env(monkeypatch, windows, own_hwnds=(), needles=()) -> None:
    monkeypatch.setattr(studio_arm, "_own_process_tree_pids", lambda: {900})
    monkeypatch.setattr(studio_arm, "_own_window_filter",
                        lambda: {"hwnds": set(own_hwnds), "needles": list(needles)})
    monkeypatch.setattr(studio_arm, "_foreground_window", lambda auto: None)
    monkeypatch.setattr(studio_arm, "_win32_top_level_windows", lambda: windows)
    monkeypatch.setattr(studio_arm, "_process_name", lambda pid: {
        77: "FTOptixStudio.exe", 500: "conhost.exe", 600: "PickerHost.exe",
        700: "notepad.exe"}.get(pid))


def test_console_host_window_outside_process_tree_is_never_blocking(monkeypatch) -> None:
    """The service's console window (GetConsoleWindow) and its root owner are
    excluded by handle even though their pid (conhost) is not in the process
    tree; so is a window titled with the python.exe path."""
    py = r"c:\python312\python.exe"
    _hint_env(monkeypatch, [
        {"title": "ftx", "pid": 500, "hwnd": 11, "class": "ConsoleWindowClass",
         "rect": (0, 0, 900, 700), "foreground": True},
        {"title": "owner", "pid": 500, "hwnd": 12, "class": "X",
         "rect": (0, 0, 900, 700), "foreground": False},
        {"title": r"C:\Python312\python.exe", "pid": 501, "hwnd": 13, "class": "X",
         "rect": (0, 0, 900, 700), "foreground": False},
        _STUDIO_WIN], own_hwnds={11, 12}, needles=[py])
    assert studio_arm.blocking_window_for(None, 77) is None
    assert studio_arm.annotate_os_dialog({"ok": False}, None, 77) == {"ok": False}


def test_console_window_excluded_on_uia_source_too(monkeypatch) -> None:
    _hint_env(monkeypatch, [_STUDIO_WIN], own_hwnds={11})
    monkeypatch.setattr(studio_arm, "_foreground_window",
                        lambda auto: {"title": "x", "pid": 500, "hwnd": 11})
    assert studio_arm.blocking_window_for(object(), 77) is None
    monkeypatch.setattr(studio_arm, "_foreground_window",
                        lambda auto: {"title": r"C:\Python312\python.exe", "pid": 500})
    monkeypatch.setattr(studio_arm, "_own_window_filter",
                        lambda: {"hwnds": set(), "needles": [r"c:\python312\python.exe"]})
    assert studio_arm.blocking_window_for(object(), 77) is None


def test_picker_dialog_is_ranked_first_with_pid_process_title_class(monkeypatch) -> None:
    _hint_env(monkeypatch, [
        {"title": "Untitled - Notepad", "pid": 700, "hwnd": 1, "class": "Notepad",
         "rect": (10, 10, 50, 50), "foreground": True},
        {"title": "Select a folder", "pid": 600, "hwnd": 2, "class": "#32770",
         "rect": (100, 100, 400, 400), "foreground": False},
        _STUDIO_WIN])
    got = studio_arm.blocking_window_for(None, 77)
    assert got == {"title": "Select a folder", "process": "PickerHost.exe",
                   "pid": 600, "class": "#32770", "source": "win32",
                   "kind": "firewall_prompt"}
    # process-name ranking without the dialog class
    _hint_env(monkeypatch, [
        {"title": "Notepad", "pid": 700, "hwnd": 1, "class": "Notepad",
         "rect": (10, 10, 50, 50), "foreground": True},
        {"title": "Pick", "pid": 600, "hwnd": 2, "class": "Windows.UI.Core.CoreWindow",
         "rect": (100, 100, 400, 400), "foreground": False},
        _STUDIO_WIN])
    assert studio_arm.blocking_window_for(None, 77)["pid"] == 600


# ---- PRD 0055 (#94): firewall / consent prompt as blocking_window ------------

_FW_HINT_TEXT = ("a Windows Firewall / consent prompt is open; answer it on the "
                 "desktop (or pre-create the firewall rule: New-NetFirewallRule "
                 "-Program <coreservicehost.exe> -Action Allow), then re-arm")

_ZERO_RECT_PICKER = {"title": "Windows Security Alert", "pid": 600, "hwnd": 2,
                     "class": "Windows.UI.Core.CoreWindow", "rect": (0, 0, 0, 0),
                     "foreground": False}


def test_zero_rect_picker_host_window_is_reported_as_firewall_prompt(monkeypatch) -> None:
    """A zero-rect PickerHost window that overlaps nothing and is not in front
    is still the blocker, with kind firewall_prompt and the identifying fields."""
    _hint_env(monkeypatch, [
        {"title": "Untitled - Notepad", "pid": 700, "hwnd": 1, "class": "Notepad",
         "rect": (10, 10, 50, 50), "foreground": True},
        _ZERO_RECT_PICKER, _STUDIO_WIN])
    got = studio_arm.blocking_window_for(None, 77)
    assert got == {"title": "Windows Security Alert", "process": "PickerHost.exe",
                   "pid": 600, "class": "Windows.UI.Core.CoreWindow",
                   "source": "win32", "kind": "firewall_prompt"}
    out = studio_arm.annotate_os_dialog({"ok": False}, None, 77)
    assert out["probable_cause"] == "os_dialog"
    assert out["blocking_window"]["kind"] == "firewall_prompt"
    assert out["hint"] == _FW_HINT_TEXT


def test_known_consent_processes_rank_above_other_overlap_candidates(monkeypatch) -> None:
    """Consent processes beat a #32770 dialog, a foreground window and a UIA
    foreground window; ShellExperienceHost counts only with a security title."""
    names = {77: "FTOptixStudio.exe", 700: "notepad.exe", 701: "notepad.exe",
             610: "consent.exe", 620: "WindowsSecurity.exe",
             630: "ShellExperienceHost.exe"}
    others = [
        {"title": "Notepad", "pid": 700, "hwnd": 1, "class": "Notepad",
         "rect": (10, 10, 50, 50), "foreground": True},
        {"title": "Some dialog", "pid": 701, "hwnd": 3, "class": "#32770",
         "rect": (100, 100, 400, 400), "foreground": False}]
    for pid, title in ((610, "User Account Control"), (620, "Firewall"),
                       (630, "Windows Security Alert")):
        _hint_env(monkeypatch, others + [
            {"title": title, "pid": pid, "hwnd": 9, "class": "X",
             "rect": (0, 0, 0, 0), "foreground": False}, _STUDIO_WIN])
        monkeypatch.setattr(studio_arm, "_process_name", names.get)
        monkeypatch.setattr(studio_arm, "_foreground_window",
                            lambda auto: {"title": "UIA fg", "pid": 700})
        got = studio_arm.blocking_window_for(object(), 77)
        assert got["pid"] == pid and got["kind"] == "firewall_prompt", got
    # ShellExperienceHost with an ordinary title is not a blocker
    _hint_env(monkeypatch, [
        {"title": "Start", "pid": 630, "hwnd": 9, "class": "X",
         "rect": (0, 0, 0, 0), "foreground": False}, _STUDIO_WIN])
    monkeypatch.setattr(studio_arm, "_process_name", names.get)
    assert studio_arm.blocking_window_for(None, 77) is None


def test_menu_item_not_found_with_zero_menu_items_runs_blocking_window_search(
        tmp_path, monkeypatch) -> None:
    """The 'menu_items: 0' arm failure always runs the search and names the
    firewall prompt, whose hint carries the New-NetFirewallRule command."""
    fake = _menu_missing_fake(tmp_path)
    _wire(monkeypatch, fake)
    _hint_env(monkeypatch, [_ZERO_RECT_PICKER, _STUDIO_WIN])
    searched = []
    real = studio_arm.blocking_window_for
    monkeypatch.setattr(studio_arm, "blocking_window_for",
                        lambda a, p: searched.append(p) or real(a, p))
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge")
    assert out["error"] == "menu_item_not_found"
    assert all(t.get("menu_items", 0) == 0 for t in out["tried"])
    assert searched
    assert out["blocking_window"]["kind"] == "firewall_prompt"
    assert out["blocking_window"]["pid"] == 600
    assert "New-NetFirewallRule" in out["hint"] and "consent prompt" in out["hint"]


# ---- PRD 0053 B (#85): bridge_port_range_full + shell windows ----------------

_RANGE = (8768, 4)


def _full_range_health(monkeypatch, project_for=lambda p: f"Other{p}") -> None:
    monkeypatch.setattr(
        studio_arm, "_bridge_health",
        lambda port, timeout=0.5: {"project": project_for(port),
                                   "studio_pid": 1000 + port - 8768})


def test_arm_pre_probe_returns_bridge_port_range_full_without_keystrokes(
        tmp_path, monkeypatch) -> None:
    """Every port in the range answers for OTHER projects: arm returns
    bridge_port_range_full with ports/owners/hint and never touches UIA."""
    _tree_project(tmp_path)
    _full_range_health(monkeypatch)
    import sys
    monkeypatch.delitem(sys.modules, "uiautomation", raising=False)
    touched: list[str] = []
    monkeypatch.setattr(studio_arm, "_studio_window_for",
                        lambda *a, **k: touched.append("window"))
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge",
                                    base_port=8768, port_range=4)
    assert out["ok"] is False and out["error"] == "bridge_port_range_full", out
    assert out["ports"] == [8768, 8769, 8770, 8771]
    assert out["owners"] == [{"port": 8768 + i, "project": f"Other{8768 + i}",
                              "pid": 1000 + i} for i in range(4)]
    assert "OPTIX_BRIDGE_PORT_RANGE" in out["hint"]
    assert "FTX_BRIDGE_PORT_RANGE" not in out["hint"]
    assert "fixed at 4" in out["hint"] and "PortRangeSize" in out["hint"]
    assert touched == []          # no window lookup, no keystrokes


def test_arm_pre_probe_not_full_when_a_port_is_free(tmp_path, monkeypatch) -> None:
    health = {8768: {"project": "A"}, 8769: {"project": "B"}, 8770: None,
              8771: {"project": "C"}}
    assert studio_arm.port_range_full_result(health, "Cell_v5") is None
    # ...and not full when one of the bridges is this project's own.
    health[8770] = {"project": "Cell_v5", "studio_pid": 5}
    assert studio_arm.port_range_full_result(health, "Cell_v5") is None
    health[8770] = {"project": "D"}
    del health[8768]
    health[8768] = {"project": "Cell_v5", "studio_pid": 5}
    assert studio_arm.port_range_full_result(health, "Cell_v5") is None
    health[8768] = {"project": "E"}
    got = studio_arm.port_range_full_result(health, "Cell_v5")
    assert got and got["error"] == "bridge_port_range_full"


def test_verify_path_scans_log_for_bind_failure_not_verify_timeout(
        tmp_path, monkeypatch) -> None:
    """After the Execute click the bridge logs its bind failure; the verify
    path reports bridge_port_range_full (with the log line) instead of
    verify_timeout, even though a foreign window ('Program Manager') is up."""
    _tree_project(tmp_path)
    fake = _TreeStudio()
    _wire_tree(monkeypatch, fake)
    monkeypatch.setattr(studio_arm, "serving_port_for", lambda *a, **k: None)
    marks: list[int] = []
    monkeypatch.setattr(studio_arm, "_log_mark", lambda: marks.append(7) or 7)
    line = ("2026-10-15 10:00:00 ERROR StudioBridge bridge_port_range_full: "
            "cannot bind any port in 8768-8771 - 4 bridges are already running")
    seen: list[object] = []
    monkeypatch.setattr(studio_arm, "_log_since",
                        lambda m: seen.append(m) or ["noise", line])
    # Only the pre-arm probe (called BEFORE the click) sees a free range.
    probes = iter([None] * 4)
    monkeypatch.setattr(studio_arm, "_bridge_health",
                        lambda port, timeout=0.5: (
                            next(probes, {"project": f"Other{port}", "pid": port})))
    monkeypatch.setattr(studio_arm, "blocking_window_for",
                        lambda *a, **k: {"title": "Program Manager"})
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge",
                                    timeout=0.05, base_port=8768, port_range=4)
    assert out["ok"] is False and out["error"] == "bridge_port_range_full", out
    assert out["detail"] == line and "blocking_window" not in out
    assert out["ports"] == [8768, 8769, 8770, 8771]
    assert [o["port"] for o in out["owners"]] == [8768, 8769, 8770, 8771]
    assert marks == [7] and seen and set(seen) == {7}


def test_verify_path_without_bind_failure_line_still_times_out(
        tmp_path, monkeypatch) -> None:
    _tree_project(tmp_path)
    _wire_tree(monkeypatch, _TreeStudio())
    monkeypatch.setattr(studio_arm, "serving_port_for", lambda *a, **k: None)
    monkeypatch.setattr(studio_arm, "_bridge_health", lambda port, timeout=0.5: None)
    monkeypatch.setattr(studio_arm, "_log_mark", lambda: 0)
    monkeypatch.setattr(studio_arm, "_log_since", lambda m: ["unrelated line"])
    out = studio_arm.execute_method("Cell_v5", str(tmp_path), method="StartBridge",
                                    timeout=0.05)
    assert out["error"] == "verify_timeout"


def test_bind_failure_line_accepts_marker_and_legacy_wording() -> None:
    f = studio_arm.bind_failure_line
    assert f(["a", "x bridge_port_range_full: cannot bind any port in 8768-8771"])
    assert f(["cannot bind any port in 8768-8771 - old bridge"])
    assert f(["listening on http://127.0.0.1:8768"]) is None
    assert f(None) is None


def test_blocking_window_for_never_returns_shell_windows(monkeypatch) -> None:
    """Progman / WorkerW / Shell_TrayWnd (+ secondary), 'Program Manager' and
    the taskbar are never a blocker on either source, even when foreground or
    overlapping Studio; a real dialog still is."""
    shell = [
        {"title": "Program Manager", "pid": 800, "hwnd": 1, "class": "Progman"},
        {"title": "", "pid": 800, "hwnd": 2, "class": "WorkerW"},
        {"title": "Taskbar", "pid": 800, "hwnd": 3, "class": "Shell_TrayWnd"},
        {"title": "Taskbar", "pid": 800, "hwnd": 4, "class": "Shell_SecondaryTrayWnd"},
        {"title": "Taskbar", "pid": 800, "hwnd": 5, "class": ""},
        {"title": "Program Manager", "pid": 800, "hwnd": 6, "class": ""},
    ]
    wins = [dict(w, rect=(0, 0, 2000, 2000), foreground=True) for w in shell]
    _hint_env(monkeypatch, wins + [_STUDIO_WIN])
    monkeypatch.setattr(studio_arm, "_process_name",
                        lambda pid: {77: "FTOptixStudio.exe", 800: "explorer.exe",
                                     700: "notepad.exe"}.get(pid))
    assert studio_arm.blocking_window_for(None, 77) is None
    for w in shell:                                   # UIA foreground source
        monkeypatch.setattr(studio_arm, "_foreground_window", lambda auto, w=w: dict(w))
        assert studio_arm.blocking_window_for(object(), 77) is None, w
        assert studio_arm.annotate_os_dialog({"ok": False}, object(), 77) == {"ok": False}
    monkeypatch.setattr(studio_arm, "_foreground_window", lambda auto: None)
    monkeypatch.setattr(studio_arm, "_win32_top_level_windows", lambda: wins + [
        _STUDIO_WIN, {"title": "Real dialog", "pid": 700, "hwnd": 9, "class": "#32770",
                      "rect": (10, 10, 50, 50), "foreground": False}])
    got = studio_arm.blocking_window_for(None, 77)
    assert got and got["pid"] == 700 and got["title"] == "Real dialog"
