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
    monkeypatch.setattr(studio_arm, "bridge_project",
                        lambda p: {8768: "Other", 8769: "Mine"}[p])
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
    assert out["ok"] is False and out["error"] == "no_optix_file"


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
        (dest / "Fresh.optix").write_text("", encoding="utf-8")
        return {"ok": True, "pid": 4321}

    monkeypatch.setattr(core, "_studio_launch", fake_launch)
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
    (d / "HasOptix.optix").write_text("", encoding="utf-8")
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
    monkeypatch.setattr(studio_arm, "bridge_project", lambda p: "Line4")
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


class _FakeStudio:
    """One Studio window, half-screen at x=961 like the measured one, with
    `Misc.` COLLAPSED: `DesignTimeNetLogic` exists only after a click lands on
    the chevron 34px left of Misc.'s text. Right-clicking Misc. opens the plain
    folder menu; right-clicking DesignTimeNetLogic opens the category menu with
    the Execute entries. Clicking `Execute StartBridge` arms."""

    def __init__(self, root_row="Cell_v5", path_label=None, show_root=True,
                 search_box=False):
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
            rows.append(_Ctl("", "EditControl", (961, 99, 349, 27)))
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
        if self.menu is not None:
            rows.append(self.menu)
        return rows

    def _row_at(self, x, y):
        for c in self._rows():
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
