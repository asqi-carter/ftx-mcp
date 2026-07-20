"""W3 granular-tool tests: list_screens, add_widget, add_model_variable,
set_property, plus the optix_model locator and optix_templates shapes.

The authored edits are run through the real deploy() path (fake runner +
export handler) so the W2 anchored-insert/find-replace engine actually
applies them — proving the generated YAML lands where intended, byte-exact.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from service import core, optix_model, optix_templates, studio_guard
from service.tests.conftest import make_export_handler, make_fake_runner, make_project

# A minimal but realistic screens file: two screens, one with an existing child.
SCREENS_YAML = """\
Name: UI
Type: UICategoryFolder
Children:
- Name: Screen1
  Id: g=11111111111111111111111111111111
  Type: Screen
  Children:
  - Name: ExistingLabel
    Type: Label
    Text: "hi"
- Name: Screen2
  Type: Screen
  Children: []
"""

MODEL_YAML = """\
Name: Model
Type: ModelFolderType
Children:
- Name: SomeExisting
  Type: BaseDataVariableType
  DataType: Int32
  Value: 0
"""


@pytest.fixture(autouse=True)
def _studio_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(studio_guard, "_scan", lambda: [])
    studio_guard.reset_cache()
    yield
    studio_guard.reset_cache()


def _project_with_ui(projects_root: Path, name: str = "Alpha") -> Path:
    proj = make_project(projects_root, name)
    (proj / "Nodes" / "UI").mkdir(parents=True)
    (proj / "Nodes" / "UI" / "Screens.yaml").write_bytes(SCREENS_YAML.encode("utf-8"))
    (proj / "Nodes" / "Model").mkdir(parents=True)
    (proj / "Nodes" / "Model" / "Model.yaml").write_bytes(MODEL_YAML.encode("utf-8"))
    return proj


def _deploy(cfg: core.Config, project: str, edits: list[dict]) -> dict:
    runner = make_fake_runner(make_export_handler())
    req = core.DeployRequest(edits=edits, commit_message="w3", run_after_deploy=False)
    return core.deploy(cfg, project, req, runner=runner)


# ---- locator ----------------------------------------------------------


def test_locator_finds_node_and_children() -> None:
    lines = SCREENS_YAML.splitlines()
    node = optix_model.find_node(lines, "Screen1", type_filter=optix_model.SCREEN_TYPES)
    assert node is not None
    assert node.node_type == "Screen"
    assert node.body_indent == 2  # dash at col 0 -> children at col 2
    assert lines[node.children_line].strip() == "Children:"


def test_locator_list_screens_counts_children() -> None:
    screens = optix_model.list_screens(SCREENS_YAML.splitlines())
    by_name = {s["name"]: s for s in screens}
    assert set(by_name) == {"Screen1", "Screen2"}
    assert by_name["Screen1"]["child_count"] == 1
    assert by_name["Screen2"]["type"] == "Screen"


def test_locator_reindent_blanks_stay_blank() -> None:
    out = optix_model.reindent("- Name: X\n\n  Type: Label", 4)
    parts = out.split("\n")
    assert parts[0] == "    - Name: X"
    assert parts[1] == ""           # blank line stays blank, no trailing pad
    assert parts[2] == "      Type: Label"


def test_templates_guid_is_unique_and_shaped() -> None:
    a, b = optix_templates.new_guid(), optix_templates.new_guid()
    assert a.startswith("g=") and len(a) == 34
    assert a != b


# ---- list_screens tool ------------------------------------------------


def test_list_screens_tool(cfg: core.Config, projects_root: Path) -> None:
    _project_with_ui(projects_root)
    out = core.list_screens(cfg, "Alpha")
    assert out["count"] == 2
    assert {s["name"] for s in out["screens"]} == {"Screen1", "Screen2"}
    assert all(s["file"] == "Nodes/UI/Screens.yaml" for s in out["screens"])


# ---- add_widget -------------------------------------------------------


def test_add_label_lands_in_screen_children(cfg: core.Config, projects_root: Path) -> None:
    proj = _project_with_ui(projects_root)
    res = core.add_widget(cfg, "Alpha", "Screen1", [
        {"kind": "label", "name": "Hello", "text": "Hello Optix", "left": 300, "top": 250},
    ])
    assert res["file"] == "Nodes/UI/Screens.yaml"
    out = _deploy(cfg, "Alpha", res["edits"])
    assert out["state"] == "succeeded"
    text = (proj / "Nodes" / "UI" / "Screens.yaml").read_text()
    # inserted as Screen1's FIRST child, before ExistingLabel, correctly indented
    assert "  - Name: Hello\n" in text
    assert '    Text: "Hello Optix"\n' in text
    assert text.index("Name: Hello") < text.index("Name: ExistingLabel")
    # Screen2 untouched
    assert "Children: []" in text


def test_add_switch_and_label_share_one_edit(cfg: core.Config, projects_root: Path) -> None:
    proj = _project_with_ui(projects_root)
    res = core.add_widget(cfg, "Alpha", "Screen1", [
        {"kind": "switch", "name": "PowerSwitch", "checked_bind": "{Model}/PowerOn"},
        {"kind": "label", "name": "OnLabel", "text": "it's on!", "visible_bind": "{Model}/PowerOn"},
    ])
    assert len(res["edits"]) == 1  # both widgets, one insert
    out = _deploy(cfg, "Alpha", res["edits"])
    assert out["state"] == "succeeded"
    text = (proj / "Nodes" / "UI" / "Screens.yaml").read_text()
    assert "  - Name: PowerSwitch\n" in text
    assert "  - Name: OnLabel\n" in text
    # the binding shape (HasDynamicLink) made it in for both
    assert text.count('HasDynamicLink, Target: "{Model}/PowerOn"') == 2


def test_add_widget_unknown_screen(cfg: core.Config, projects_root: Path) -> None:
    _project_with_ui(projects_root)
    with pytest.raises(core.ScreenNotFound):
        core.add_widget(cfg, "Alpha", "Nope", [{"kind": "label", "name": "X", "text": "y"}])


def test_add_widget_rejects_bad_param(cfg: core.Config, projects_root: Path) -> None:
    _project_with_ui(projects_root)
    with pytest.raises(core.WidgetSpecInvalid):
        core.add_widget(cfg, "Alpha", "Screen1", [
            {"kind": "label", "name": "X", "text": "y", "bogus": 1},
        ])


def test_add_widget_unknown_kind(cfg: core.Config, projects_root: Path) -> None:
    _project_with_ui(projects_root)
    with pytest.raises(core.WidgetSpecInvalid):
        core.add_widget(cfg, "Alpha", "Screen1", [{"kind": "gauge", "name": "X"}])


# ---- add_model_variable -----------------------------------------------


def test_add_model_variable_lands(cfg: core.Config, projects_root: Path) -> None:
    proj = _project_with_ui(projects_root)
    res = core.add_model_variable(cfg, "Alpha", "PowerOn")
    assert res["target_path"] == "{Model}/PowerOn"
    out = _deploy(cfg, "Alpha", res["edits"])
    assert out["state"] == "succeeded"
    text = (proj / "Nodes" / "Model" / "Model.yaml").read_text()
    # bare export-safe shape: the PowerOn node is exactly Name/Type/DataType
    # with NO AccessLevel/Value/Id before the next node (W4 finding — those
    # hang Studio export on FactoryTalk-template projects)
    assert (
        "- Name: PowerOn\n  Type: BaseDataVariableType\n  DataType: Boolean\n- Name: SomeExisting"
        in text
    )


def test_add_model_variable_rejects_non_boolean(cfg: core.Config, projects_root: Path) -> None:
    _project_with_ui(projects_root)
    with pytest.raises(core.StructuralEditUnsupported):
        core.add_model_variable(cfg, "Alpha", "Speed", datatype="Int32")


# ---- empty-project shapes: create Children when absent (W4 finding) ----

EMPTY_MODEL = "Name: Model\nType: ModelCategoryFolder\n"  # fresh-project stub, no Children


def test_add_model_variable_creates_children_on_empty_model(
    cfg: core.Config, projects_root: Path
) -> None:
    proj = make_project(projects_root, "Alpha")
    (proj / "Nodes" / "UI").mkdir(parents=True)
    (proj / "Nodes" / "UI" / "Screens.yaml").write_bytes(SCREENS_YAML.encode("utf-8"))
    (proj / "Nodes" / "Model").mkdir(parents=True)
    (proj / "Nodes" / "Model" / "Model.yaml").write_bytes(EMPTY_MODEL.encode("utf-8"))
    res = core.add_model_variable(cfg, "Alpha", "PowerOn")
    out = _deploy(cfg, "Alpha", res["edits"])
    assert out["state"] == "succeeded"
    text = (proj / "Nodes" / "Model" / "Model.yaml").read_text()
    # Children block was created at body indent 0, with the variable under it
    assert "Children:\n- Name: PowerOn\n" in text
    assert "  DataType: Boolean\n" in text
    assert "AccessLevel" not in text  # bare export-safe shape


def test_add_widget_converts_empty_inline_children(cfg: core.Config, projects_root: Path) -> None:
    # Screen2 in the fixture has `Children: []` — adding a widget must convert
    # it to a block list, not append a malformed sibling.
    proj = _project_with_ui(projects_root)
    res = core.add_widget(cfg, "Alpha", "Screen2", [
        {"kind": "label", "name": "First", "text": "hi"},
    ])
    out = _deploy(cfg, "Alpha", res["edits"])
    assert out["state"] == "succeeded"
    text = (proj / "Nodes" / "UI" / "Screens.yaml").read_text()
    assert "Children: []" not in text          # the inline-empty was converted
    assert "  Children:\n  - Name: First\n" in text


# ---- set_property -----------------------------------------------------


def test_set_property_changes_inline_value(cfg: core.Config, projects_root: Path) -> None:
    proj = _project_with_ui(projects_root)
    res = core.set_property(
        cfg, "Alpha", "Nodes/UI/Screens.yaml", "ExistingLabel", "Text", '"changed"'
    )
    assert res["old_value"] == '"hi"'
    out = _deploy(cfg, "Alpha", res["edits"])
    assert out["state"] == "succeeded"
    text = (proj / "Nodes" / "UI" / "Screens.yaml").read_text()
    assert '    Text: "changed"\n' in text
    assert '    Text: "hi"' not in text


def test_set_property_missing_widget(cfg: core.Config, projects_root: Path) -> None:
    _project_with_ui(projects_root)
    with pytest.raises(core.NodeNotFound):
        core.set_property(cfg, "Alpha", "Nodes/UI/Screens.yaml", "Ghost", "Text", '"x"')


def test_set_property_non_inline_is_structural(cfg: core.Config, projects_root: Path) -> None:
    # 'Children' is not an inline scalar property -> structural refusal
    _project_with_ui(projects_root)
    with pytest.raises(core.StructuralEditUnsupported):
        core.set_property(cfg, "Alpha", "Nodes/UI/Screens.yaml", "Screen1", "Nonexistent", "1")


# ---- guard still applies to authoring ---------------------------------


def test_authoring_refused_while_studio_open(
    cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_with_ui(projects_root)
    monkeypatch.setattr(studio_guard, "_scan",
                        lambda: [{"pid": 1, "name": "ftoptixstudio.exe", "cmdline": []}])
    studio_guard.reset_cache()
    with pytest.raises(core.StudioOpen):
        core.list_screens(cfg, "Alpha")
    with pytest.raises(core.StudioOpen):
        core.add_widget(cfg, "Alpha", "Screen1", [{"kind": "label", "name": "X", "text": "y"}])
