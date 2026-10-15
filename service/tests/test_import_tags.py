"""import_tags: design-time Tag Importer op + optix_bridge_import_tags tool.

Offline (no Studio, no dotnet): the pure helpers, argument validation, request
shaping / error mapping through a faked bridge, and source-level guards on the
C# route (the bench build_check is the only compile gate). The live tests are in
test_bridge_live.py. Reference: NetLogic_CheatSheet pages/designtime-tags-import.md.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from service import auth, core
from service.tests.conftest import make_project
from service.tests.test_bridge_writes import _bridge_cs_text, _cs_region, _fake_bridge

FIXTURE = Path(__file__).parent / "fixtures" / "import_tags_sample.L5X"


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.fixture
def alpha(cfg, projects_root):
    proj = make_project(projects_root, "Alpha")
    # the service pre-flight needs a real L5X for every offline import
    for rel in ("a.L5X", "plc/a.L5X"):
        dest = proj / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(FIXTURE.read_bytes())
    return cfg


# -- filter semantics --------------------------------------------------------

@pytest.mark.parametrize("symbol,requested,expected", [
    ("Motor1.Speed", ["Motor1.Speed"], True),                   # exact
    ("Motor1", ["Motor1.Limits.High"], True),                   # parent of a request
    ("Motor1.Limits", ["Motor1.Limits.High"], True),            # parent (nested)
    ("Motor1.Limits.High", ["Motor1.Limits.High"], True),       # exact (nested)
    ("Motor1.Limits.High", ["Motor1"], True),                   # child of a request
    ("Motor1.Limits", ["Motor1"], True),
    ("Motor10", ["Motor1"], False),                             # no false prefix
    ("Motor10.Speed", ["Motor1"], False),
    ("Motor1", ["Motor10"], False),                             # ...in either direction
    ("Motor1.Speed", ["Motor1.Spee"], False),                   # partial member name
    ("Motor1.Speed", ["Motor1.Speed2"], False),
    ("Motor1.Mode", ["Motor1.Speed"], False),                   # sibling
    ("Other", ["Motor1", "Other"], True),                       # any of several
    ("Motor1", [], False),
    ("", ["Motor1"], False),
])
def test_import_tags_filter_semantics(symbol, requested, expected):
    assert core.import_tags_symbol_matches(symbol, requested) is expected


def test_import_tags_filter_exact_parent_child_and_no_false_prefix():
    req = ["Motor1.Limits.High"]
    syms = ["Motor1", "Motor1.Limits", "Motor1.Limits.High", "Motor1.Limits.High.Raw",
            "Motor1.Limits.Low", "Motor1.Speed", "Motor10", "Motor10.Limits.High"]
    kept = [s for s in syms if core.import_tags_symbol_matches(s, req)]
    assert kept == ["Motor1", "Motor1.Limits", "Motor1.Limits.High",
                    "Motor1.Limits.High.Raw"]


def test_csharp_filter_mirrors_python_boundaries():
    body = _cs_region(_bridge_cs_text(), "private static bool ImportTagsSymbolMatches(",
                      "private static string ImportTagsVersionGate(")
    assert 'symbol.StartsWith(r + "."' in body       # child, on a '.' boundary
    assert 'r.StartsWith(symbol + "."' in body       # parent, on a '.' boundary
    assert "StringComparison.Ordinal" in body and "OrdinalIgnoreCase" not in body


# -- version gate ------------------------------------------------------------

@pytest.mark.parametrize("kind,ver,expected", [
    ("ra", "1.7.4.32", "requires_optix_1_8"),
    ("ra", "1.7", "requires_optix_1_8"),
    ("ra", "1.8.0.555", None),
    ("ra", "1.9.1", None),
    ("ra", "2.0.0", None),
    ("twincat", "1.7.4.32", None),          # TwinCAT works from 1.7
    ("twincat", "1.8.0.555", None),
    ("ra", "unknown", None),                # cannot prove it is old: not refused
    ("ra", None, None),
])
def test_import_tags_version_gate(kind, ver, expected):
    assert core.import_tags_version_gate(kind, ver) == expected


def test_csharp_version_gate_uses_the_pow_gate_source():
    text = _bridge_cs_text()
    gate = _cs_region(text, "private static string ImportTagsVersionGate(",
                      "private static string ImportTagsStationKind(")
    assert 'stationKind != "ra"' in gate and "VersionAtLeast(studioVersion, ImportTagsRaMinVersion)" in gate
    assert 'ImportTagsRaMinVersion = "1.8"' in text
    handler = _cs_region(text, "private string ImportTagsInline(", "// Structural check for a translation key")
    assert "StudioVersionString()" in handler                    # same source as the pow gate
    assert handler.index("ImportTagsVersionGate(") < handler.index('GetProperty("TagImporter"')


# -- argument validation -----------------------------------------------------

_OK = dict(station="CommDrivers/D/S", mode="offline", file="a.L5X", tags=["Motor1"])


def _errs(**over):
    args = {**_OK, **over}
    return [(e["code"], e["field"]) for e in core.validate_import_tags_op(
        args["station"], args["mode"], args["file"], args["tags"], args.get("timeout_s"))]


def test_validate_import_tags_accepts_well_formed():
    assert _errs() == []
    assert _errs(mode="ONLINE", file=None) == []            # file only needed offline
    assert _errs(mode="Offline", timeout_s=30) == []
    assert _errs(mode="online", file="ignored.L5X") == []


@pytest.mark.parametrize("over,expected", [
    ({"station": None}, [("missing_field", "station")]),
    ({"station": "  "}, [("missing_field", "station")]),
    ({"station": 5}, [("missing_field", "station")]),
    ({"mode": None}, [("missing_field", "mode")]),
    ({"mode": "sideways"}, [("invalid_mode", "mode")]),
    ({"file": None}, [("missing_field", "file")]),
    ({"file": ""}, [("missing_field", "file")]),
    ({"file": 3}, [("invalid_file", "file")]),
    ({"tags": None}, [("missing_field", "tags")]),
    ({"tags": []}, [("missing_field", "tags")]),
    ({"tags": "Motor1"}, [("invalid_tags", "tags")]),
    ({"tags": ["Motor1", ""]}, [("invalid_tags", "tags")]),
    ({"tags": ["Motor1", 4]}, [("invalid_tags", "tags")]),
    ({"tags": [" Motor1"]}, [("invalid_tags", "tags")]),
    ({"timeout_s": 0}, [("invalid_timeout", "timeout_s")]),
    ({"timeout_s": -5}, [("invalid_timeout", "timeout_s")]),
    ({"timeout_s": "soon"}, [("invalid_timeout", "timeout_s")]),
    ({"timeout_s": True}, [("invalid_timeout", "timeout_s")]),
    ({"timeout_s": 10 ** 6}, [("invalid_timeout", "timeout_s")]),
])
def test_validate_import_tags_structured_errors(over, expected):
    assert _errs(**over) == expected


def test_invalid_mode_lists_allowed():
    err = core.validate_import_tags_op("S", "x", "f", ["t"])[0]
    assert err["allowed"] == ["offline", "online"]


def test_bad_request_is_refused_before_the_bridge(alpha, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("bridge must not be called")
    monkeypatch.setattr(core, "_bridge_http", boom)
    monkeypatch.setattr(core, "_bridge_post_body", boom)
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["Motor1"])
    assert out["ok"] is False and out["code"] == "missing_field"
    assert out["errors"][0]["field"] == "file"


# -- request shaping + result / error mapping --------------------------------

class _Post:
    def __init__(self, status=200, data=None):
        self.calls: list = []
        self.status, self.data = status, data if data is not None else {}

    def __call__(self, cfg, path, payload, timeout=20.0):
        self.calls.append((path, payload, timeout))
        return self.status, self.data


def test_bridge_import_tags_posts_body_with_bounded_default_timeout(alpha, monkeypatch):
    post = _Post(data={"ok": True, "added": ["CommDrivers/D/S/Tags/Motor1"],
                       "already_present": [], "not_found": ["Nope"], "elapsed_ms": 42})
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", post)
    out = core.bridge_import_tags(alpha, "Alpha", "CommDrivers/D/S", "Offline",
                                  ["Motor1", "Nope"], file="plc/a.L5X")
    assert out["ok"] is True and out["added"] == ["CommDrivers/D/S/Tags/Motor1"]
    assert out["not_found"] == ["Nope"] and out["elapsed_ms"] == 42
    path, payload, timeout = post.calls[0]
    assert path.startswith("/bridge/station/import-tags")
    assert payload == {"station": "CommDrivers/D/S", "mode": "offline",
                       "tags": ["Motor1", "Nope"], "timeout_s": 120.0, "file": "plc/a.L5X",
                       "source_symbols": ["Motor1", "Motor10"]}
    assert timeout > 120.0                       # HTTP wait outlasts the bridge's own bound


def test_bridge_import_tags_online_drops_file_and_honours_timeout(alpha, monkeypatch):
    post = _Post(data={"ok": True})
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", post)
    out = core.bridge_import_tags(alpha, "Alpha", "S", "online", ["T"], file="x.L5X",
                                  timeout_s=15)
    assert "file" not in post.calls[0][1] and post.calls[0][1]["timeout_s"] == 15.0
    assert post.calls[0][1]["mode"] == "online"
    assert out["added"] == [] and out["already_present"] == [] and out["not_found"] == []


def test_requires_optix_1_8_is_a_structured_refusal(alpha, monkeypatch):
    err = {"error": {"code": "requires_optix_1_8", "message":
                     "the RA EtherNet/IP tag importer needs Optix Studio 1.8 or newer "
                     "(running 1.7.4.32); TwinCAT stations import from 1.7"}}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", _Post(status=200, data=err))
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["T"], file="a.L5X")
    assert out["ok"] is False and out["code"] == "requires_optix_1_8"
    assert out["requires_version"] == {"min_version": "1.8", "studio_version": "1.7.4.32"}
    assert out["errors"][0]["field"] == "station"


@pytest.mark.parametrize("code,msg", [
    ("import_failed", "Unable to parse the L5X file"),
    ("import_cancelled", "A task was canceled."),
    ("import_timeout", "the importer did not finish within 5 s"),
    ("file_not_found", "no such import file: C:\\x.L5X"),
    ("station_not_found", "no node at: CommDrivers/Nope"),
    ("not_a_tag_station", "CommDrivers/X is a Folder, not an RA EtherNet/IP or TwinCAT station"),
])
def test_importer_failures_report_code_and_base_message(alpha, monkeypatch, code, msg):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body",
                        _Post(data={"error": {"code": code, "message": msg}}))
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["T"], file="a.L5X")
    assert out["ok"] is False and out["code"] == code
    assert msg in out["message"]


def test_unknown_bridge_failure_still_raises(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body",
                        _Post(data={"error": {"code": "weird", "message": "boom"}}))
    with pytest.raises(core.BridgeWriteFailed):
        core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["T"], file="a.L5X")


# -- batch surface -----------------------------------------------------------

def test_import_tags_is_an_edit_op():
    assert "import_tags" in core.BRIDGE_EDIT_VERBS
    fn, required, optional = core._BRIDGE_EDIT_OPS["import_tags"]
    assert fn == "bridge_import_tags" and required == ("station", "mode", "tags")
    assert set(optional) == {"file", "timeout_s", "replace", "dry_run"}
    assert core._BRIDGE_EDIT_POSITIONAL["import_tags"] == required
    assert core.unknown_op_fields({"op": "import_tags", **_OK, "bogus": 1}) == ["bogus"]


def test_edit_op_applies_through_the_same_function(alpha, monkeypatch):
    post = _Post(data={"ok": True, "added": ["p"]})
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", post)
    res = core._apply_one_edit(alpha, "Alpha", {"op": "import_tags", **_OK})
    assert res["added"] == ["p"] and post.calls[0][1]["tags"] == ["Motor1"]


def test_edit_op_refusal_counts_as_failed_op(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    with pytest.raises(core.BridgeWriteFailed, match="missing_field"):
        core._apply_one_edit(alpha, "Alpha", {"op": "import_tags", "station": "S",
                                              "mode": "offline", "tags": ["x"], "file": " "})


def test_batch_dry_run_rejects_bad_import_tags(alpha, monkeypatch):
    ok_report = (200, {"ok": True, "errors": [], "warnings": [], "ops": 1})
    monkeypatch.setattr(core, "_bridge_post_body", lambda cfg, path, payload, **kw: ok_report)
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    bad = core.bridge_edit(alpha, "Alpha", [
        {"op": "import_tags", "station": "S", "mode": "sideways", "tags": []}], dry_run=True)
    assert bad["report"]["ok"] is False and bad["applied"] == 0
    codes = {e.get("code") for e in bad["report"]["errors"]}
    assert {"invalid_mode", "missing_field"} <= codes


def test_csharp_validate_ops_knows_import_tags():
    case = _cs_region(_bridge_cs_text(), 'case "import_tags":', 'case "add_translations":')
    for needle in ("invalid_mode", "unresolved_reference", "modifies_deleted_node", '"tags"'):
        assert needle in case, needle


# -- the direct tool ---------------------------------------------------------

def test_direct_tool_registered_as_gated_author_tool(cfg, monkeypatch):
    from service.mcp_app import make_mcp
    tool = "optix_bridge_import_tags"
    assert auth.TOOL_SCOPES.get(tool) == "author"
    assert tool not in {t.name for t in make_mcp(cfg)._tool_manager.list_tools()}
    monkeypatch.setenv("FTXMCP_BRIDGE_PRIMITIVES", "1")
    tools = {t.name: t for t in make_mcp(cfg)._tool_manager.list_tools()}
    assert tool in tools
    props = tools[tool].parameters["properties"]
    assert {"station", "mode", "tags", "file", "timeout_s"} <= set(props)
    assert set(tools[tool].parameters["required"]) == {"station", "mode", "tags"}


# -- C# route: reflection only, bounded wait, base exception ------------------

def test_csharp_route_registered_and_logged():
    text = _bridge_cs_text()
    assert 'StartsWith("POST /bridge/station/import-tags")' in text
    assert '"POST /bridge/station/import-tags"' in text.split("_MutationRoutes", 1)[1][:800]
    assert '",\\"import_tags\\":\\"1\\""' in text            # advertised in /bridge/health


def test_csharp_handler_is_reflection_only_so_a_1_7_compile_builds():
    text = _bridge_cs_text()
    code = _cs_region(text, "private string ImportTagsInline(", "// Structural check for a translation key")
    # no static reference to the 1.8-only importer types in the whole file
    assert not re.search(r"^\s*using\s+(static\s+)?[\w.]*(RAEtherNetIP|TagImporterAPI|TwinCAT)",
                         text, re.M)
    assert "TagImporterAPI." not in re.sub(r"//[^\n]*", "", text)
    for needle in ('GetProperty("TagImporter"', 'ImportTagsFindProperty(itype, "FilePath")',
                   'ImportTagsFindProperty(itype, "Mode")', '"FromAbsoluteFilePath"', "Enum.Parse(", 'm.Name != "Import"'):
        assert needle in code or needle in text, needle


def test_csharp_api_mismatch_error_carries_importer_diagnostics():
    text = _bridge_cs_text()
    code = _cs_region(text, "private string ImportTagsInline(", "// Structural check for a translation key")
    desc = _cs_region(text, "private static string ImportTagsDescribeImporter(",
                      "// Station-side detail:")
    # runtime type full name, public property names, every Import* method with parameter types
    assert "FullName" in text.split("private static string ImportTagsTypeName(", 1)[1][:200]
    assert "importer runtime type: " in desc and "ImportTagsTypeName(t)" in desc
    assert "GetProperties(BindingFlags.Public | BindingFlags.Instance)" in desc
    assert 'StartsWith("Import", StringComparison.Ordinal)' in desc
    assert "GetParameters()" in desc and "ParameterType" in desc
    # the mismatch branch names the missing members and appends both reports
    mismatch = code.split("TagImporter API mismatch", 1)[1][:600]
    assert "ImportTagsDescribeImporter(importer)" in mismatch
    assert "ImportTagsDesignTimeProbe(station, stationPath)" in mismatch
    assert "Import(Func<string,string,bool>)" in code


def test_csharp_station_side_error_says_whether_design_time_node_exists_and_where_it_looked():
    text = _bridge_cs_text()
    code = _cs_region(text, "private string ImportTagsInline(", "// Structural check for a translation key")
    probe = _cs_region(text, "private static string ImportTagsDesignTimeProbe(",
                       "private string ImportTagsInline(")
    assert "DesignTimeNodes" not in probe
    assert '"/TagImporter (station child)"' in probe
    assert "EXISTS" in probe and "NOT FOUND" in probe and "searched: [" in probe
    # getter exception, missing property and null importer all carry the probe
    assert code.count("ImportTagsDesignTimeProbe(station, stationPath)") >= 4
    assert "station.TagImporter getter threw" in code


def test_csharp_diagnostics_have_no_static_1_8_references():
    text = _bridge_cs_text()
    probe = _cs_region(text, "private static PropertyInfo ImportTagsFindProperty(",
                       "private string ImportTagsInline(")
    assert not re.search(r"RAEtherNetIP|TagImporterAPI|TwinCAT", probe)


def test_csharp_wait_is_bounded_and_off_the_ui_thread():
    text = _bridge_cs_text()
    code = _cs_region(text, "private string ImportTagsInline(", "// Structural check for a translation key")
    assert "task.Wait(TimeSpan.FromSeconds(timeoutS))" in code        # bounded
    assert "ImportTagsDefaultTimeoutS = 120.0" in text
    assert re.search(r"\.Wait\(\)|\.Result\b|GetAwaiter\(\)\.GetResult", code) is None
    assert "ImpersonateRootTemporary()" in code                        # listener thread: root session
    assert "DelayedTask" not in code and "Dispatch(" not in code      # never marshalled to the UI thread


def test_csharp_reports_faulted_and_cancelled_with_base_exception():
    code = _cs_region(_bridge_cs_text(), "private string ImportTagsInline(",
                      "// Structural check for a translation key")
    assert "task.IsCanceled" in code and "task.IsFaulted" in code
    assert 'ErrorJson("import_cancelled"' in code and 'ErrorJson("import_failed"' in code
    assert code.count("GetBaseException()") >= 3
    assert 'ErrorJson("import_timeout"' in code


def test_csharp_result_fields_from_before_after_snapshots():
    code = _cs_region(_bridge_cs_text(), "private string ImportTagsInline(",
                      "// Structural check for a translation key")
    assert code.index("before, 0") < code.index("importM.Invoke(") < code.index("after, 0")
    for key in ('\\"added\\"', '\\"already_present\\"', '\\"not_found\\"', '\\"elapsed_ms\\"'):
        assert key in code, key


# -- the L5X fixture ---------------------------------------------------------

def test_l5x_fixture_shape():
    root = ET.parse(FIXTURE).getroot()
    assert root.tag == "RSLogix5000Content"
    controllers = root.findall("Controller")
    assert len(controllers) == 1
    udts = {d.get("Name"): d for d in controllers[0].findall("DataTypes/DataType")}
    assert "Motor_UDT" in udts
    # the UDT's nested struct member is typed by another data type of the file
    members = {m.get("DataType"): m.get("Name") for m in udts["Motor_UDT"].findall("Members/Member")}
    nested = [dt for dt in members if dt in udts]
    assert nested == ["Motor_Limits"]
    assert [m.get("Name") for m in udts["Motor_Limits"].findall("Members/Member")] == ["Low", "High"]
    tags = [t.get("Name") for t in controllers[0].findall("Tags/Tag")]
    assert tags == ["Motor1", "Motor10"]                   # Motor1 vs Motor10: the false-prefix pair
    for t in controllers[0].findall("Tags/Tag"):
        assert t.get("DataType") == "Motor_UDT"


def test_csharp_dry_run_refuses_ra_station_on_studio_below_1_8():
    """validate_ops (dry run) applies the same version gate as the apply route."""
    case = _cs_region(_bridge_cs_text(), 'case "import_tags":', 'case "add_translations":')
    assert "ImportTagsStationKind(itLive)" in case
    assert "ImportTagsVersionGate(itKind, itVer)" in case
    assert "StudioVersionString()" in case
    assert 'addErr(idx, "requires_optix_1_8"' in case
    # the unresolved-station check still runs for a station that does not exist
    assert "unresolved_reference" in case


def test_dry_run_surfaces_requires_optix_1_8_from_bridge(alpha, monkeypatch):
    """A bridge dry-run report carrying requires_optix_1_8 comes back unchanged."""
    report = {"ok": False, "ops": 1, "warnings": [], "errors": [
        {"op_index": 0, "code": "requires_optix_1_8", "field": "station",
         "message": "the RA EtherNet/IP tag importer needs Optix Studio 1.8 or newer "
                    "(running 1.7.4.32); TwinCAT stations import from 1.7"}]}
    monkeypatch.setattr(core, "_bridge_post_body",
                        lambda cfg, path, payload, **kw: (200, report))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    res = core.bridge_edit(alpha, "Alpha", [{"op": "import_tags", **_OK}], dry_run=True)
    assert res["applied"] == 0
    assert {e.get("code") for e in res["report"]["errors"]} == {"requires_optix_1_8"}


# ---- find-or-create design-time TagImporter (PRD 0038 req. 2/3; ftx-mcp-aaw.2) ----

def _ensure_region():
    return _cs_region(_bridge_cs_text(), "private string ImportTagsEnsureImporter(",
                      "private static string ImportTagsStringArray(")


def _inline_region():
    return _cs_region(_bridge_cs_text(), "private string ImportTagsInline(",
                      "// Structural check for a translation key")


def test_csharp_import_tags_ensures_importer_before_reading_the_getter():
    code = _inline_region()
    assert "ImportTagsEnsureImporter(station, stationPath, kind," in code
    assert code.index("ImportTagsEnsureImporter(") < code.index("tiProp.GetValue(station)")


def test_csharp_importer_type_resolved_by_exact_browsename_under_base_type():
    ens = _cs_region(_bridge_cs_text(), "private static IUANode ImportTagsFindTypeBelow(",
                     "private static string ImportTagsStringArray(")
    code = re.sub(r"//[^\n]*", "", ens)
    assert "string.Equals(cur.BrowseName, name, StringComparison.Ordinal)" in code
    assert "IndexOf(" not in code and ".Contains(" not in code        # no substring match
    assert 'ImportTagsBaseTypeName = "TagImporter"' in _bridge_cs_text()
    assert '"RAEtherNetIPTagImporter"' in code and '"TwinCATTagImporter"' in code
    assert "ImportTagsFindTypeBelow(baseType, typeName)" in code       # subtypes of the base only
    assert not re.search(r"new NodeId\(|ResolveUiTypeId|ObjectTypes\.TagImporter", code)  # no hard-coded id


def test_csharp_existing_importer_is_used_and_never_deleted():
    ens = _ensure_region()
    first = ens.split("ImportTagsTryGetImporter(station, out err) != null) return \"existing\"", 1)
    assert len(first) == 2
    pre = ens.split("private string ImportTagsEnsureImporter(", 1)[1].split('return "existing"', 1)[0]
    assert "Delete" not in pre and "Add(" not in pre
    inline = _inline_region()
    assert "tagImporterState" in _bridge_cs_text()
    # deletion only ever targets the node this call created
    assert "ImportTagsDeleteNode(tempImporter, station)" in inline
    assert "if (tempImporter != null && !timedOut)" in inline


def test_csharp_temporary_importer_named_added_to_station_and_verified():
    ens = _ensure_region()
    assert "InformationModel.MakeObject(ImportTagsTempName, typeNode.NodeId)" in ens
    assert 'ImportTagsTempName = "FtxMcpTagImporter"' in _bridge_cs_text()
    assert "station.Add(imp)" in ens
    assert ens.index("station.Add(imp)") < ens.index("ImportTagsTryGetImporter(station, out err) != null", ens.index("station.Add(imp)"))
    assert 'return "temporary"' in ens


def test_csharp_temporary_importer_deleted_except_on_timeout():
    inline = _inline_region()
    # every outcome of the run (success, fault, cancel, validation error, throw) reaches the delete
    assert "runJson = ImportTagsRun(" in inline
    assert "catch (Exception ex)" in inline.split("runJson = ImportTagsRun(", 1)[1].split("ImportTagsDeleteNode", 1)[0]
    assert "tempImporter != null && !timedOut" in inline
    run = _bridge_cs_text().split("private string ImportTagsRun(", 1)[1]
    assert "timedOut = true" in run and "tag_importer_left" in run


def test_csharp_unresolved_type_or_failing_getter_cleans_up_and_returns_import_failed():
    ens = _ensure_region()
    assert 'return "failed"' in ens
    assert "ImportTagsDeleteNode(imp, station)" in ens
    assert ens.index("ImportTagsDeleteNode(imp, station)") > ens.index("station.Add(imp)")
    assert "routes tried: [" in ens
    inline = _inline_region()
    failed = inline.split('ensured == "failed"', 1)[1][:400]
    assert 'ErrorJson("import_failed"' in failed
    assert "ImportTagsDesignTimeProbe(station, stationPath)" in failed
    assert "ImportTagsUiStationHint" in failed
    assert "station created in Studio's UI" in _bridge_cs_text().split("ImportTagsUiStationHint =", 1)[1][:300]


def test_csharp_create_object_no_longer_creates_a_tag_importer():
    text = _bridge_cs_text()
    co = _cs_region(text, "private string CreateObjectInline(", "// POST /bridge/model/netlogic")
    assert "ImportTagsEnsureImporter(" not in co
    assert "ImportTagsStationKind" not in co
    assert "ImportTagsTryGetImporter" not in co
    assert "tag_importer" not in co
    assert "DesignTimeNodes" not in _bridge_cs_text()


def test_import_timeout_error_carries_tag_importer_left(alpha, monkeypatch):
    msg = ("the importer did not finish within 5 s. The temporary importer was kept. "
           "tag_importer_left: CommDrivers/D/S/FtxMcpTagImporter")
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", _Post(data={"error": {
        "code": "import_timeout", "message": msg,
        "tag_importer_left": "CommDrivers/D/S/FtxMcpTagImporter"}}))
    out = core.bridge_import_tags(alpha, "Alpha", "CommDrivers/D/S", "offline", ["T"], file="a.L5X")
    assert out["ok"] is False and out["code"] == "import_timeout"
    assert out["tag_importer_left"] == "CommDrivers/D/S/FtxMcpTagImporter"
    assert out["errors"][0]["tag_importer_left"] == "CommDrivers/D/S/FtxMcpTagImporter"


def test_import_result_reports_tag_importer_state(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", _Post(data={
        "ok": True, "tag_importer": "temporary", "added": [], "already_present": [],
        "not_found": [], "elapsed_ms": 1}))
    out = core.bridge_import_tags(alpha, "Alpha", "CommDrivers/D/S", "offline", ["T"], file="a.L5X")
    assert out["tag_importer"] == "temporary"


def test_csharp_ensure_importer_is_reflection_only_for_1_7():
    text = _bridge_cs_text()
    region = _cs_region(text, "private static object ImportTagsTryGetImporter(",
                        "private static string ImportTagsStringArray(")
    code = re.sub(r"//[^\n]*", "", region)
    code = re.sub(r'"[^"\n]*"', '""', code)           # type NAMES as strings are fine
    assert not re.search(r"RAEtherNetIP|TagImporterAPI|TwinCAT", code)


def test_failed_creation_surfaces_hint_and_diagnostics(alpha, monkeypatch):
    msg = ("could not create the design-time TagImporter for CommDrivers/D/S; routes tried: "
           "[station.TagImporter: boom]. hint: a station created in Studio's UI has its TagImporter")
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body",
                        _Post(data={"error": {"code": "import_failed", "message": msg}}))
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["T"], file="a.L5X")
    assert out["ok"] is False and out["code"] == "import_failed"
    assert "routes tried" in out["message"]
    assert "created in Studio's UI" in out["hint"]


# ---- tolerant Import delegate binding (ftx-mcp-aaw.4) ----

def _binding_code():
    return _cs_region(_bridge_cs_text(), "private static bool ImportTagsIsFilterDelegate(",
                      "private string ImportTagsInline(")


def test_csharp_import_binds_any_string_string_bool_delegate_type():
    code = _binding_code()
    # shape check on Invoke(), not on the Func<> type identity
    assert 'GetMethod("Invoke")' in code and "ReturnType != typeof(bool)" in code
    assert code.count("typeof(string)") >= 2
    # the exact delegate type is built from the filter's closure method
    assert "Delegate.CreateDelegate(delegateType, filter.Target, filter.Method)" in code
    inline = _cs_region(_bridge_cs_text(), "private string ImportTagsInline(",
                        "// Structural check for a translation key")
    assert "ImportTagsBuildFilterDelegate(" in inline
    assert "new object[] { filter }" not in inline


def test_csharp_parameterless_import_only_when_tags_empty():
    code = _binding_code()
    body = code.split("private static void ImportTagsFindImport(", 1)[1]
    assert "if (tagsEmpty && plainM != null)" in body
    # plainM is referenced nowhere else as a result
    assert body.count("method = plainM") == 1
    inline = _cs_region(_bridge_cs_text(), "private string ImportTagsInline(",
                        "// Structural check for a translation key")
    assert "ImportTagsFindImport(itype, tags.Count == 0," in inline


def test_csharp_mode_and_filepath_are_read_from_base_types():
    text = _bridge_cs_text()
    find = _cs_region(text, "private static PropertyInfo ImportTagsFindProperty(",
                      "private static string ImportTagsTypeName(")
    assert "t = t.BaseType" in find and "BindingFlags.DeclaredOnly" in find
    inline = _cs_region(text, "private string ImportTagsInline(", "// Structural check for a translation key")
    assert 'ImportTagsFindProperty(itype, "Mode")' in inline
    assert 'ImportTagsFindProperty(itype, "FilePath")' in inline


# ---- batch validator: a station type creates Tags/Types (PRD 0038 req. 8) ----

def _validate_region():
    return _cs_region(_bridge_cs_text(), "private string ValidateOpsJson(", "// POST")


def test_csharp_batch_validator_station_creates_tags_and_types_children():
    text = _bridge_cs_text()
    helper = _cs_region(text, "private List<string> HypoStationTypeChildren(",
                        "// Resolve a path against the hypothetical tree")
    # declared children are used when the type resolves; Tags/Types are always present
    assert "typeNode.Children" in helper and "c.BrowseName" in helper
    assert '"Tags", "Types"' in helper
    assert "RAEtherNetIP" in helper and "TwinCAT" in helper
    # the create case registers them in the hypothetical model after the station
    body = text.split("private string ValidateOpsJson(", 1)[1]
    create = body.split('case "create_widget":', 1)[0]
    assert "HypoStationTypeChildren(typeName)" in create
    assert create.index("created[newPath] = new HypoNode") < create.index("HypoStationTypeChildren(typeName)")
    assert 'created[newPath + "/" + stc]' in create


def test_csharp_batch_validator_still_reports_unresolved_parent_for_unknown_parents():
    text = _bridge_cs_text()
    body = text.split("private string ValidateOpsJson(", 1)[1]
    create = body.split('case "create_widget":', 1)[0]
    # the parent check still precedes the station-children registration and is unchanged
    assert 'addErr(idx, "unresolved_parent"' in create
    # only station types add children; any other type adds nothing
    helper = text.split("private List<string> HypoStationTypeChildren(", 1)[1].split("private string HypoResolve", 1)[0]
    assert 'EndsWith("Station"' in helper and "return null;" in helper


# ---- ftx-mcp-aaw.9: acceptance tests for the TagImporter work -------------------
# One named test per acceptance item. They complement the finer-grained tests above;
# the C# side has no offline compiler, so those checks read the bridge source.

def test_import_failure_diagnostics_shape(alpha, monkeypatch):
    """An importer API mismatch reaches the caller as a structured refusal whose
    message carries the importer runtime type, its properties and Import* signatures."""
    diag = ("TagImporter API mismatch: no usable Import(Func<string,string,bool>); "
            "importer runtime type: FTOptix.RAEtherNetIP.TagImporter; "
            "properties: [FilePath, Mode]; methods: [Import(Func`3)]; "
            "design-time node: NOT FOUND, searched: [S/TagImporter (station child)]")
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body",
                        _Post(data={"error": {"code": "import_failed", "message": diag}}))
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["T"], file="a.L5X")
    assert out["ok"] is False and out["code"] == "import_failed"
    assert set(out) >= {"ok", "code", "message", "errors", "nudge"}
    assert out["errors"] and out["errors"][0]["code"] == "import_failed"
    for needle in ("importer runtime type: ", "properties: [", "methods: [", "searched: ["):
        assert needle in out["message"], needle
    # and the C# producer emits each piece of that shape
    cs = _bridge_cs_text()
    desc = _cs_region(cs, "private static string ImportTagsDescribeImporter(", "// Station-side detail:")
    assert "importer runtime type: " in desc and "GetParameters()" in desc
    probe = _cs_region(cs, "private static string ImportTagsDesignTimeProbe(",
                       "private string ImportTagsInline(")
    assert "EXISTS" in probe and "NOT FOUND" in probe and "searched: [" in probe


def test_import_delegate_binds_custom_delegate_type_with_same_signature():
    """The filter is bound by Invoke() shape (string,string)->bool, then rebuilt as
    the importer's own delegate type, so a custom delegate type still binds."""
    code = _binding_code()
    shape = code.split("private static bool ImportTagsIsFilterDelegate(", 1)[1].split("private static", 1)[0]
    assert 'GetMethod("Invoke")' in shape and "ReturnType != typeof(bool)" in shape
    assert shape.count("typeof(string)") >= 2
    assert "Func<" not in shape                       # no identity check on Func<,,>
    assert "Delegate.CreateDelegate(delegateType, filter.Target, filter.Method)" in code


def test_import_tags_dry_run_refusal_requires_optix_1_8():
    """Dry run on Studio < 1.8 refuses an RA station via the same gate as apply."""
    assert core.import_tags_version_gate("ra", "1.7.4.32") == "requires_optix_1_8"
    assert core.import_tags_version_gate("ra", "1.8.0.555") is None
    case = _cs_region(_bridge_cs_text(), 'case "import_tags":', 'case "add_translations":')
    assert "ImportTagsVersionGate(itKind, itVer)" in case
    assert 'addErr(idx, "requires_optix_1_8"' in case


def test_import_tags_dry_run_reports_single_error(alpha, monkeypatch):
    """A refused import_tags dry run yields exactly one error: the service validator
    stays quiet for a well-formed op and the bridge's refusal is not duplicated."""
    assert core.validate_import_tags_op("S", "offline", "a.L5X", ["T"]) == []
    one = {"op_index": 0, "code": "requires_optix_1_8", "field": "station",
           "message": "the RA EtherNet/IP tag importer needs Optix Studio 1.8 or newer "
                      "(running 1.7.4.32)"}
    report = {"ok": False, "ops": 1, "warnings": [], "errors": [one]}
    monkeypatch.setattr(core, "_bridge_post_body", lambda cfg, path, payload, **kw: (200, report))
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    res = core.bridge_edit(alpha, "Alpha", [{"op": "import_tags", **_OK}], dry_run=True)
    assert [e["code"] for e in res["report"]["errors"]] == ["requires_optix_1_8"]
    # create_object of a variable type: the bridge validator must not repeat the service's
    assert 'addErr(idx, "variable_type_not_object"' not in _bridge_cs_text()


def test_batch_validator_accepts_station_then_tags_path():
    """validate_ops: after create_object of a station type, <station>/Tags and
    <station>/Types resolve, so a following op on <station>/Tags/<tag> is not
    reported as an unresolved parent."""
    text = _bridge_cs_text()
    helper = _cs_region(text, "private List<string> HypoStationTypeChildren(",
                        "// Resolve a path against the hypothetical tree")
    assert '"Tags", "Types"' in helper
    create = text.split("private string ValidateOpsJson(", 1)[1].split('case "create_widget":', 1)[0]
    assert 'created[newPath + "/" + stc]' in create
    assert create.index("HypoStationTypeChildren(typeName)") > create.index("created[newPath] = new HypoNode")


# ---- ftx-mcp-jku.3: import_tags result fields in optix_bridge_edit entries ------

def _batch_entry(alpha, monkeypatch, data, ops=None):
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    import_post = _Post(data=data)

    def post(cfg, path, payload, timeout=20.0):
        if "import-tags" in path:
            return import_post(cfg, path, payload, timeout)
        # validate_ops / batch plumbing: a clean report
        return 200, {"ok": True, "op_count": 1, "strict": False, "errors": [], "warnings": []}

    monkeypatch.setattr(core, "_bridge_post_body", post)
    out = core.bridge_edit(alpha, "Alpha", ops or [{"op": "import_tags", **_OK}])
    entry = next(t for t in out["op_timings"] if t["op"] == "import_tags")
    return out, entry


def test_batch_import_tags_entry_carries_result_fields(alpha, monkeypatch):
    out, entry = _batch_entry(alpha, monkeypatch, {
        "ok": True, "added": ["CommDrivers/D/S/Tags/Motor1"], "already_present": ["X"],
        "not_found": ["Nope"], "elapsed_ms": 42, "tag_importer": "temporary"})
    assert entry["ok"] is True
    assert entry["added"] == ["CommDrivers/D/S/Tags/Motor1"]
    assert entry["already_present"] == ["X"]
    assert entry["not_found"] == ["Nope"]
    assert entry["elapsed_ms"] == 42
    assert entry["tag_importer"] == "temporary"


def test_batch_import_tags_entry_carries_error_code_and_detail(alpha, monkeypatch):
    out, entry = _batch_entry(alpha, monkeypatch, {
        "error": {"code": "import_failed", "message": "boom"}})
    assert entry["ok"] is False
    assert entry["code"] == "import_failed"
    assert "boom" in entry["detail"]
    # the failing op was the first: nothing applied -> "failed", model unchanged
    assert out["state"] == "failed" and out["failed_op"]["op"] == "import_tags"


def test_batch_non_import_op_entries_are_unchanged():
    t = core._op_timing(0, {"op": "set_property"}, 5, {"ok": True, "added": ["x"]})
    assert t == {"index": 0, "op": "set_property", "ms": 5, "ok": True}
    f = core._failed_op_timing(1, {"op": "set_property"}, 5, ValueError("x"))
    assert f == {"index": 1, "op": "set_property", "ms": 5, "ok": False}


def test_batch_validator_still_accepts_station_and_tags_path_in_one_batch():
    """0038: the C# dry-run validator registers Tags/Types under a created station;
    the python side must not reject the same batch (source-level + python gate)."""
    case = _cs_region(_bridge_cs_text(), 'case "import_tags":', 'case "add_translations":')
    assert "HypoResolve" in case or "created" in case
    assert core.validate_import_tags_op("CommDrivers/D/S", "offline", "a.L5X", ["T"]) == []


# ---- ftx-mcp-jku.6: source-level tests for the temporary station importer ---------
# The C# bridge cannot be compiled or run here, so (as above) these read its source.
# The live counterparts are test_bridge_live.py -k "import_tags" (1.8) and
# test_import_tags_dry_run_requires_optix_1_8_on_1_7_live (1.7): pending bench verification.

def _strip_cs_comments(code: str) -> str:
    return re.sub(r"//[^\n]*", "", code)


def _run_region() -> str:
    return _cs_region(_bridge_cs_text(), "private string ImportTagsRun(",
                      "// Structural check for a translation key")


def test_importer_type_resolution_is_exact_browsename_never_substring():
    """The importer type is the subtype of the TagImporter base type whose BrowseName
    is EXACTLY RAEtherNetIPTagImporter / TwinCATTagImporter: no substring, prefix,
    suffix or case-insensitive match anywhere in the resolution helpers."""
    region = _cs_region(_bridge_cs_text(), "private static IUANode ImportTagsFindTypeBelow(",
                        "private static string ImportTagsDeleteNode(")
    code = _strip_cs_comments(region)
    for loose in (".Contains(", "IndexOf(", ".StartsWith(", ".EndsWith(", "Regex",
                  "IgnoreCase", "CurrentCulture", ".Like("):
        assert loose not in code, loose
    # both the base type lookup and the importer type lookup use the exact helper
    assert "ImportTagsFindTypeBelow(objRoot, ImportTagsBaseTypeName)" in code
    assert "ImportTagsFindTypeBelow(baseType, typeName)" in code
    assert code.count("string.Equals(cur.BrowseName, name, StringComparison.Ordinal)") == 1
    # a missing base type or subtype is reported, never guessed at
    assert "not found in the type tree" in code and "has BrowseName exactly" in code
    ens = _ensure_region()
    assert 'kind == "ra" ? "RAEtherNetIPTagImporter" : "TwinCATTagImporter"' in ens


def test_existing_importer_is_used_and_never_deleted():
    """A station that already has a TagImporter is used as is: the helper returns
    "existing" before it creates anything, `created` stays null, and every delete in
    the file targets only a node this call itself made (imp / tempImporter)."""
    text = _strip_cs_comments(_bridge_cs_text())
    ens = _strip_cs_comments(_ensure_region())
    head = ens.split('return "existing"', 1)[0]
    assert "created = null;" in head
    for mutation in ("MakeObject", ".Add(", "ImportTagsDeleteNode", "created = "):
        assert mutation not in head.replace("created = null;", ""), mutation
    assert ens.count("created = imp;") == 1 and ens.index("created = imp;") > ens.index("station.Add(imp)")
    calls = re.findall(r"ImportTagsDeleteNode\((\w+(?:\.\w+)*), station\)", text)
    assert sorted(calls) == ["imp", "tempImporter"], calls
    # the caller only deletes what the helper handed back as created
    inline = _strip_cs_comments(_inline_region())
    assert "if (tempImporter != null && !timedOut)" in inline
    assert inline.count("ImportTagsDeleteNode(") == 1
    # no other route deletes a station's importer (no Delete on the station's TagImporter value)
    assert not re.search(r"TagImporter[^;\n]*\.Delete\(", text)


def test_temporary_importer_deleted_on_success_fault_and_validation_error():
    """Every outcome of the run other than a timeout reaches the one delete in the
    caller: a successful import, a cancelled/faulted task, an API-mismatch (validation)
    error, a refused file/Import call, and an exception thrown by the run."""
    inline = _strip_cs_comments(_inline_region())
    run = _strip_cs_comments(_run_region())
    # the delete lives in the caller, after the run, outside the try that wraps it
    assert inline.index("runJson = ImportTagsRun(") < inline.index("ImportTagsDeleteNode(tempImporter, station)")
    after_run = inline.split("runJson = ImportTagsRun(", 1)[1]
    assert after_run.index("catch (Exception ex)") < after_run.index("ImportTagsDeleteNode(")
    assert 'runJson = ErrorJson("import_failed", ex.GetBaseException().Message);' in after_run
    # the run itself never deletes (so no early return can skip, or duplicate, the cleanup)
    assert "ImportTagsDeleteNode" not in run
    # success, faulted, cancelled and validation-error results are all plain returns of the run
    assert 'return "{\\"ok\\":true' in run
    assert 'return ErrorJson("import_failed", be != null' in run
    assert 'return ErrorJson("import_cancelled"' in run
    assert "TagImporter API mismatch" in run
    # only the timeout branch can switch the cleanup off
    assert run.count("timedOut = true;") == 1
    assert run.index("timedOut = true;") > run.index("if (!finished)")
    assert run.index("timedOut = true;") < run.index("if (task.IsCanceled)")
    # a cleanup failure is reported, not swallowed
    assert "ImportTagsAddLeftField(runJson, tempPath, del)" in inline


def test_temporary_importer_kept_and_reported_on_timeout():
    """On import_timeout the temporary importer is NOT deleted (the import may still be
    running against it) and the reply names it in tag_importer_left."""
    run = _strip_cs_comments(_run_region())
    timeout = run.split("if (!finished)", 1)[1].split("if (task.IsCanceled)", 1)[0]
    assert "timedOut = true;" in timeout
    assert "ImportTagsDeleteNode" not in timeout
    assert "The temporary importer was kept." in timeout
    assert 'tag_importer_left' in timeout and "tempPath" in timeout
    assert '\\"code\\":\\"import_timeout\\"' in timeout
    assert "tempImporter != null && !timedOut" in _strip_cs_comments(_inline_region())


def test_version_gate_runs_before_any_importer_is_created():
    """On Studio 1.7 an RA station is refused before ImportTagsEnsureImporter could add
    anything to it."""
    inline = _inline_region()
    assert inline.index("ImportTagsVersionGate(") < inline.index("ImportTagsEnsureImporter(")
    assert 'ErrorJson("requires_optix_1_8"' in inline


def test_ensure_helper_has_one_caller_and_is_not_on_the_create_object_path():
    """Only import_tags calls the ensure helper; creating a station (create_object, in
    the single route and in a batch) no longer creates or touches a TagImporter."""
    text = _strip_cs_comments(_bridge_cs_text())
    assert len(re.findall(r"ImportTagsEnsureImporter\(", text)) == 2        # definition + the one call
    call_site = text.index("ImportTagsEnsureImporter(station, stationPath, kind,")
    inline_start = text.index("private string ImportTagsInline(")
    assert inline_start < call_site
    co = _strip_cs_comments(_cs_region(_bridge_cs_text(), "private string CreateObjectInline(",
                                       "// POST /bridge/model/netlogic"))
    for needle in ("ImportTagsEnsureImporter", "ImportTagsTempName", "ImportTagsResolveImporterType",
                   "ImportTagsFindTypeBelow", "ImportTagsDeleteNode", "TagImporter"):
        assert needle not in co, needle
    # the batch create_object op is bridge_create_object -> the same route
    assert core._BRIDGE_EDIT_OPS["create_object"][0] == "bridge_create_object"


def test_import_tags_result_fields_in_route_and_batch_entry():
    """The route emits added / already_present / not_found / elapsed_ms / tag_importer
    and the optix_bridge_edit entry copies exactly those (plus tag_importer_left)."""
    run = _run_region()
    for key in ('\\"tag_importer\\":\\"" + tagImporterState', '\\"added\\":', '\\"already_present\\":',
                '\\"not_found\\":', '\\"removed\\":', '\\"skipped\\":', '\\"elapsed_ms\\":'):
        assert key in run, key
    assert set(core._IMPORT_TAGS_RESULT_KEYS) == {
        "added", "already_present", "not_found", "removed", "skipped", "elapsed_ms",
        "tag_importer", "tag_importer_left"}
    full = {"ok": True, "added": ["a"], "already_present": ["b"], "not_found": ["c"],
            "elapsed_ms": 7, "tag_importer": "existing", "matched_symbols": 3, "via": "tag-importer"}
    entry = core._op_timing(2, {"op": "import_tags"}, 9, full)
    assert {k: entry[k] for k in core._IMPORT_TAGS_RESULT_KEYS if k in entry} == {
        "added": ["a"], "already_present": ["b"], "not_found": ["c"],
        "elapsed_ms": 7, "tag_importer": "existing"}
    assert "matched_symbols" not in entry and "via" not in entry
    left = core._op_timing(0, {"op": "import_tags"}, 1, {"ok": True, "tag_importer_left": "S/FtxMcpTagImporter"})
    assert left["tag_importer_left"] == "S/FtxMcpTagImporter"


@pytest.mark.parametrize("module_type", [
    "FTOptix.RAEtherNetIP.RAEtherNetIPDriver",       # BrowseName spelling
    "FTOptix.RAEtherNetIP.Driver",                   # ObjectTypes catalog field spelling
    "FTOptix.RAEtherNetIP.RAEtherNetIPStation",
    "FTOptix.RAEtherNetIP.Station",
])
def test_module_type_spellings_reach_the_bridge_unchanged(alpha, monkeypatch, module_type):
    """The service passes either Module.Type spelling through verbatim; resolution is the
    bridge's job (see the C# test below)."""
    cap: list = []
    routes = {"/bridge/model/object": (200, {"ok": True, "created_path": "CommDrivers/D",
                                              "type": module_type, "node_class": "Object"})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    out = core.bridge_create_object(alpha, "Alpha", "CommDrivers", "D", object_type=module_type)
    assert out["ok"] is True
    _, path = next(c for c in cap if "/bridge/model/object" in c[1])
    assert "type=" + module_type in path


def test_csharp_module_type_resolves_catalog_field_and_browsename_spellings():
    """ResolveModuleTypeId tries the module's ObjectTypes catalog FIELD name first
    (FTOptix.RAEtherNetIP.Driver) and, on a miss for a pinned Module.Type, falls back to the
    BrowseName within that module's namespace (FTOptix.RAEtherNetIP.RAEtherNetIPDriver)."""
    text = _bridge_cs_text()
    body = _cs_region(text, "private static NodeId ResolveModuleTypeId(",
                      "private static NodeId ResolveModuleTypeByBrowseName(")
    code = _strip_cs_comments(body)
    assert "t.GetField(field," in code and "return nid;" in code
    assert code.index("t.GetField(field,") < code.index("ResolveModuleTypeByBrowseName(ns, field)")
    assert "if (ns != null) return ResolveModuleTypeByBrowseName(ns, field);" in code
    fb = _strip_cs_comments(_cs_region(text, "private static NodeId ResolveModuleTypeByBrowseName(",
                                       "private static int ModuleNamespaceIndex("))
    assert "ModuleNamespaceIndex(ns)" in fb and "FindTypeByBrowseName(searchRoot, browseName, nsIdx)" in fb
    # the dry run (validate_ops) and the apply route share the one resolver
    assert "ResolveModuleTypeId(name)" in _cs_region(text, "private NodeId ResolveUiTypeId(",
                                                      "private static NodeId ResolveModuleTypeId(")
    # station detection works off the resolved CLR proxy type, so both spellings count as a station
    kind = _cs_region(text, "private static string ImportTagsStationKind(", "// path -> SymbolName")
    assert 'EndsWith("Station"' in kind and "FTOptix.RAEtherNetIP" in kind


# ---- PRD 0050 A (#69): merge by default, pre-flight, would-remove, honest result ----

def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _no_bridge(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("bridge must not be called")
    monkeypatch.setattr(core, "_bridge_http", boom)
    monkeypatch.setattr(core, "_bridge_post_body", boom)


_TPY = (b'<?xml version="1.0"?><PlcProjectInfo><Symbols>'
        b'<Symbol><Name>MAIN.Motor1</Name></Symbol><Symbol><Name>MAIN.Pump</Name></Symbol>'
        b'</Symbols></PlcProjectInfo>')


@pytest.mark.parametrize("name,data,needle", [
    ("empty.L5X", b"", "empty"),
    ("tags.csv", b"Name,Type\nMotor1,DINT\n", "not valid XML"),
    ("notxml.L5X", b"this is not xml at all", "not valid XML"),
    ("wrongroot.L5X", b"<?xml version='1.0'?><Other><Tags/></Other>", "unexpected XML root"),
    ("latin1.L5X", b"<?xml version='1.0' encoding='UTF-8'?><RSLogix5000Content>"
                   b"<Controller><Tags><Tag Name='Mot\xf6r1'/></Tags></Controller>"
                   b"</RSLogix5000Content>", "not valid XML"),
])
def test_invalid_import_file_is_refused_without_a_bridge_call(
        alpha, monkeypatch, tmp_path, name, data, needle):
    path = _write(tmp_path, name, data)
    _no_bridge(monkeypatch)
    out = core.bridge_import_tags(alpha, "Alpha", "CommDrivers/D/S", "offline",
                                  ["Motor1"], file=str(path))
    assert out["ok"] is False and out["error"] == "invalid_import_file"
    assert out["path"] == str(path) and needle in out["detail"]


def test_missing_import_file_is_invalid_import_file(alpha, monkeypatch):
    _no_bridge(monkeypatch)
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["T"], file="plc/nope.L5X")
    assert out["error"] == "invalid_import_file" and "does not exist" in out["detail"]
    assert out["path"].replace("\\", "/").endswith("Alpha/plc/nope.L5X")


def test_batch_dry_run_rejects_invalid_import_file(alpha, monkeypatch, tmp_path):
    path = _write(tmp_path, "empty.L5X", b"")
    ok_report = (200, {"ok": True, "errors": [], "warnings": [], "ops": 1})
    monkeypatch.setattr(core, "_bridge_post_body", lambda cfg, path, payload, **kw: ok_report)
    monkeypatch.setattr(core, "_use_bridge_for", lambda cfg, project: True)
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    bad = core.bridge_edit(alpha, "Alpha", [
        {"op": "import_tags", **{**_OK, "file": str(path)}}], dry_run=True)
    assert bad["report"]["ok"] is False and bad["applied"] == 0
    err = next(e for e in bad["report"]["errors"] if e.get("code") == "invalid_import_file")
    assert err["path"] == str(path) and err["field"] == "file"


def test_source_symbols_from_l5x_and_tpy(tmp_path):
    syms, err = core.import_tags_source_symbols(FIXTURE)
    assert err is None and syms == ["Motor1", "Motor10"]
    tpy = _write(tmp_path, "p.tpy", _TPY)
    syms, err = core.import_tags_source_symbols(tpy)
    assert err is None and syms == ["MAIN.Motor1", "MAIN.Pump"]


def test_l5x_program_scoped_tags_are_source_symbols(tmp_path):
    p = _write(tmp_path, "prog.L5X", b"<RSLogix5000Content><Controller><Tags><Tag Name='A'/></Tags>"
               b"<Programs><Program><Tags><Tag Name='B'/></Tags></Program></Programs>"
               b"</Controller></RSLogix5000Content>")
    assert core.import_tags_source_symbols(p) == (["A", "B"], None)


def test_valid_source_file_sends_source_symbols_and_no_replace_query(alpha, monkeypatch):
    post = _Post(data={"ok": True})
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    monkeypatch.setattr(core, "_bridge_post_body", post)
    core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["Motor1"], file="a.L5X")
    path, payload, _ = post.calls[0]
    assert "replace" not in path and payload["source_symbols"] == ["Motor1", "Motor10"]


def test_replace_true_builds_the_bridge_query_with_replace_1(alpha, monkeypatch):
    post = _Post(data={"ok": True, "removed": ["S/Tags/Old"]})
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", post)
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["Motor1"],
                                  file="a.L5X", replace=True)
    path, payload, _ = post.calls[0]
    assert path.startswith("/bridge/station/import-tags") and "replace=1" in path
    assert out["removed"] == ["S/Tags/Old"]


def test_replace_must_be_a_bool(alpha, monkeypatch):
    _no_bridge(monkeypatch)
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["T"], file="a.L5X",
                                  replace="yes")
    assert out["ok"] is False and out["code"] == "invalid_replace"


def test_batch_op_passes_replace_through(alpha, monkeypatch):
    post = _Post(data={"ok": True})
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))

    def route(cfg, path, payload, timeout=20.0):
        if "import-tags" in path:
            return post(cfg, path, payload, timeout)
        return 200, {"ok": True, "op_count": 1, "strict": False, "errors": [], "warnings": []}
    monkeypatch.setattr(core, "_bridge_post_body", route)
    core.bridge_edit(alpha, "Alpha", [{"op": "import_tags", **_OK, "replace": True}])
    assert "replace=1" in post.calls[0][0]


def test_would_remove_tags_is_refused_with_removed_would_be_and_hint(alpha, monkeypatch):
    """The bridge (which holds the station's `before` snapshot) refuses with the
    list; the service turns it into the structured refusal."""
    post = _Post(status=200, data={"error": {
        "code": "would_remove_tags", "message": '["Legacy","Old.Thing"]'}})
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", post)
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["Motor1"], file="a.L5X")
    assert out["ok"] is False and out["error"] == "would_remove_tags"
    assert out["removed_would_be"] == ["Legacy", "Old.Thing"]
    assert out["hint"] == ("pass replace=true to accept, or import from a file "
                           "that still contains them")


def test_would_remove_helper_lists_station_tags_the_file_lacks():
    present = ["Motor1", "Motor1.Speed", "Legacy", "Legacy.A", "Pump"]
    assert core.import_tags_would_remove(present, ["Motor1", "Motor10"]) == ["Legacy", "Pump"]
    assert core.import_tags_would_remove(present, ["Motor1", "Legacy", "Pump"]) == []
    # file symbol is a child of a station tag: the station tag is a parent, kept
    assert core.import_tags_would_remove(["MAIN"], ["MAIN.Motor1"]) == []
    assert core.import_tags_would_remove([], ["X"]) == []


def test_default_merge_filter_keeps_already_present_symbols():
    present = {"Motor1", "Motor1.Speed", "Legacy"}
    req = ["Motor10"]
    assert core.import_tags_keeps_symbol("Motor10.Speed", req, present) is True   # requested
    assert core.import_tags_keeps_symbol("Legacy", req, present) is True          # present
    assert core.import_tags_keeps_symbol("Motor1.Speed", req, present) is True    # present
    assert core.import_tags_keeps_symbol("Other", req, present) is False          # neither


def test_replace_filter_keeps_only_requested_tags():
    present = {"Motor1", "Legacy"}
    req = ["Motor10"]
    assert core.import_tags_keeps_symbol("Motor10", req, present, replace=True) is True
    assert core.import_tags_keeps_symbol("Legacy", req, present, replace=True) is False
    assert core.import_tags_keeps_symbol("Motor1", req, present, replace=True) is False


def test_result_passes_removed_and_skipped_through(alpha, monkeypatch):
    skipped = [{"symbol": "Bad", "reason": "unknown DataType"}]
    post = _Post(data={"ok": True, "added": [], "removed": ["S/Tags/Old"], "skipped": skipped})
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}))
    monkeypatch.setattr(core, "_bridge_post_body", post)
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["Bad"], file="a.L5X",
                                  replace=True)
    assert out["removed"] == ["S/Tags/Old"] and out["skipped"] == skipped
    bare = _Post(data={"ok": True})
    monkeypatch.setattr(core, "_bridge_post_body", bare)
    out = core.bridge_import_tags(alpha, "Alpha", "S", "offline", ["T"], file="a.L5X")
    assert out["removed"] == [] and out["skipped"] == []


def test_batch_entry_carries_removed_skipped_and_refusal_fields(alpha, monkeypatch):
    skipped = [{"symbol": "Bad", "reason": "r"}]
    _, entry = _batch_entry(alpha, monkeypatch, {"ok": True, "removed": ["p"], "skipped": skipped})
    assert entry["removed"] == ["p"] and entry["skipped"] == skipped
    _, entry = _batch_entry(alpha, monkeypatch, {"error": {
        "code": "would_remove_tags", "message": '["Legacy"]'}})
    assert entry["ok"] is False and entry["code"] == "would_remove_tags"
    assert entry["removed_would_be"] == ["Legacy"]


def test_csharp_filter_merge_replace_guard_and_result_fields():
    text = _bridge_cs_text()
    run = _cs_region(text, "private string ImportTagsRun(", "// Structural check for a translation key")
    assert 'QueryParam(firstLine, "replace")' in text
    assert "ImportTagsKeepsSymbol(symbolName, tags, present, replace)" in run
    assert "ImportTagsWouldRemove(present, sourceSymbols)" in run
    # the guard runs on the before snapshot, ahead of the import call
    assert run.index("before, 0") < run.index('ErrorJson("would_remove_tags"') < run.index("importM.Invoke(")
    assert "!replace && sourceSymbols != null" in run
    assert 'ErrorJson("would_remove_tags"' in run
    helper = _cs_region(text, "private static bool ImportTagsKeepsSymbol(",
                        "private static bool ImportTagsSymbolMatches(")
    assert "!replace && present != null" in helper
    comment = _cs_region(text, "// The importer's symbol filter (mirror of core.import_tags_keeps_symbol)",
                         "private static bool ImportTagsKeepsSymbol(")
    assert "ALREADY PRESENT" in comment and "replace=true: keep only" in comment


def test_python_filter_docstring_states_the_merge_rule():
    doc = core.import_tags_symbol_matches.__doc__
    assert "replace=False" in doc and "already present" in doc and "replace=True" in doc
