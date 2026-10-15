"""attach_* `sources` handling and the attach_string_formatter placeholder guard.

Live finding (2026-09-24): an agent passed `source`
(singular) to attach_string_formatter. It was only an unknown_op_field
warning, so it was dropped; the formatter attached with no Source0 and the
batch reported `succeeded`. Passing `sources` as a JSON list was stringified
to "['Model/X']" and failed source_not_variable.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from service import core
from service.tests.conftest import make_project
from service.tests.test_bridge_writes import _fake_bridge, _fake_validate

_OK_REPORT = {"ok": True, "op_count": 1, "strict": False, "errors": [], "warnings": []}
_SFMT = {"op": "attach_string_formatter", "path": "UI/MainWindow/L1",
         "prop_name": "Text", "format": "{0:F2}"}


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    return cfg


@pytest.mark.parametrize("fmt,sources,missing", [
    ("{0:F2}", None, [0]),
    ("{0:F2}", "", [0]),
    ("{0:F2}", "Model/T", []),
    ("{0} / {1}", "Model/A", [1]),
    ("{0} / {1}", ["Model/A", "Model/B"], []),
    ("{1,8:N1}", "Model/A", [1]),
    ("{{literal}} {0}", "Model/A", []),
    ("{{0}}", None, []),
    ("no placeholders", None, []),
])
def test_unbound_placeholders(fmt, sources, missing):
    assert core.unbound_placeholders(fmt, sources) == missing


def test_coerce_sources_joins_lists_per_verb():
    assert core._coerce_sources("attach_string_formatter", ["A", " B ", ""]) == "A,B"
    assert core._coerce_sources("attach_expression", ("A", "B")) == "A,B"
    assert core._coerce_sources("attach_formatter", ["n=A", "m=B"]) == "n=A;m=B"
    assert core._coerce_sources("attach_formatter", {"n": "A"}) == "n=A"
    assert core._coerce_sources("attach_expression", "A,B") == "A,B"
    with pytest.raises(core.BridgeWriteFailed, match="positional"):
        core._coerce_sources("attach_string_formatter", {"n": "A"})


def test_normalize_aliases_source_to_sources():
    out = core._normalize_edit_op({**_SFMT, "source": "Model/T"})
    assert out["sources"] == "Model/T" and "source" not in out
    assert core.unknown_op_fields(out) == []


def test_normalize_joins_list_sources():
    op = {**_SFMT, "sources": ["Model/A", "Model/B"]}
    out = core._normalize_edit_op(op)
    assert out["sources"] == "Model/A,Model/B"
    assert op["sources"] == ["Model/A", "Model/B"], "caller op must not be mutated"


def test_normalize_rejects_conflicting_source_and_sources():
    with pytest.raises(core.BridgeWriteFailed, match="plural"):
        core._normalize_edit_op({**_SFMT, "source": "Model/A", "sources": "Model/B"})


def test_normalize_leaves_clean_op_identical():
    op = {**_SFMT, "name": "Text", "sources": "Model/T"}
    assert core._normalize_edit_op(op) is op


def test_per_noun_refuses_unbound_placeholder_before_http(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed, match="unbound_placeholder"):
        core.bridge_attach_string_formatter(alpha, "Alpha", "UI/MainWindow/L1",
                                            "Text", "{0:F2}")
    assert not any("attach-string-formatter" in p for _m, p in cap)


def test_per_noun_sends_joined_list_sources(alpha, monkeypatch):
    cap: list = []
    routes = {"/bridge/node/attach-string-formatter": (200, {"ok": True})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_attach_string_formatter(alpha, "Alpha", "UI/MainWindow/L1", "Text",
                                        "{0} {1}", ["Model/A", "Model/B"])
    _m, p = next(c for c in cap if "attach-string-formatter" in c[1])
    assert "sources=Model%2FA%2CModel%2FB" in p


def test_bridge_edit_unbound_placeholder_is_an_error_even_non_strict(alpha, monkeypatch):
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(dict(_OK_REPORT)))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op) or {"ok": True})
    out = core.bridge_edit(alpha, "Alpha", [dict(_SFMT)])
    codes = [e["code"] for e in out["report"]["errors"]]
    assert "unbound_placeholder" in codes and out["report"]["ok"] is False
    assert applied == []


def test_bridge_edit_singular_source_applies_bound(alpha, monkeypatch):
    seen: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(dict(_OK_REPORT), seen=seen))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op) or {"ok": True})
    out = core.bridge_edit(alpha, "Alpha", [{**_SFMT, "source": "Model/GuardValue"}])
    assert out["report"]["errors"] == [] and out["report"]["warnings"] == []
    assert applied and applied[0]["sources"] == "Model/GuardValue"
    # the C# validator sees the normalized op too
    assert seen[0][1]["ops"][0]["sources"] == "Model/GuardValue"


def test_bridge_guard_is_before_any_model_write():
    """Static check on the C#: the unbound_placeholder return must precede the
    first model mutation in AttachStringFormatterInline."""
    src = (Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs").read_text(
        encoding="utf-8-sig")
    body = src[src.index("private string AttachStringFormatterInline"):]
    body = body[:body.index("\n    }\n")]
    assert body.index('"unbound_placeholder"') < body.index("ResolveNode(path)")
    assert '\\"placeholders\\":' in body and '\\"bound\\":' in body


# ---- format_path_not_runtime_resolvable (PRD 0023 #1) ------------------------

_AFMT = {"op": "attach_formatter", "path": "UI/MainWindow/L1", "prop_name": "Text",
         "format": "Model/Demo/{#pick}", "sources": "pick=Model/Demo/Pick"}
_REWRITE = "/Objects/<Project>/Model/Demo/{#pick}"


@pytest.mark.parametrize("fmt,kind", [
    ("Model/Demo/{#pick}", "project_relative"),
    ("UI/Screens/{#k}@NodeId", "project_relative"),
    ("Converters/{#k}", "project_relative"),
    ("NetLogic/X", "project_relative"),
    ("/Objects/Alpha/Model/Demo/{#pick}", "runtime_resolvable"),
    ("../NavPanel{#i}@NodeId", "runtime_resolvable"),
    ("./Child{#i}", "runtime_resolvable"),
    ("{#a}/{#b}", "runtime_resolvable"),
    ("", "runtime_resolvable"),
])
def test_classify_format_path(fmt, kind):
    assert core.classify_format_path(fmt) == kind


def test_format_path_rewrite():
    assert core.format_path_rewrite("Model/X/{#k}", "Alpha") == "/Objects/Alpha/Model/X/{#k}"
    assert core.format_path_rewrite("Model/X/{#k}") == "/Objects/<Project>/Model/X/{#k}"


@pytest.mark.parametrize("strict", [False, True])
def test_attach_formatter_project_relative_format_path_is_an_error(alpha, monkeypatch, strict):
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(dict(_OK_REPORT)))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_reported_project", lambda cfg, project: "Alpha")
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op) or {"ok": True})
    out = core.bridge_edit(alpha, "Alpha", [dict(_AFMT)], strict=strict)
    errs = [e for e in out["report"]["errors"]
            if e["code"] == "format_path_not_runtime_resolvable"]
    assert errs and out["report"]["ok"] is False
    assert _REWRITE in errs[0]["message"]
    assert "/Objects/Alpha/Model/Demo/{#pick}" in errs[0]["message"]
    assert applied == []


def test_per_noun_attach_formatter_refuses_project_relative_before_write(alpha, monkeypatch):
    cap: list = []
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge({}, capture=cap))
    with pytest.raises(core.BridgeWriteFailed,
                       match="format_path_not_runtime_resolvable") as ei:
        core.bridge_attach_formatter(alpha, "Alpha", "UI/MainWindow/L1", "Text",
                                     "Model/Demo/{#pick}", "pick=Model/Demo/Pick")
    assert _REWRITE in str(ei.value)
    assert not any("attach-formatter" in p for _m, p in cap)


@pytest.mark.parametrize("fmt", [
    "/Objects/Alpha/Model/Demo/{#pick}", "../Panel{#pick}@NodeId", "./Panel{#pick}",
    "{#pick}",
])
def test_per_noun_attach_formatter_accepts_runtime_resolvable(alpha, monkeypatch, fmt):
    cap: list = []
    routes = {"/bridge/node/attach-formatter": (200, {"ok": True})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_attach_formatter(alpha, "Alpha", "UI/MainWindow/L1", "Text", fmt,
                                 "pick=Model/Demo/Pick")
    assert any("attach-formatter" in p for _m, p in cap)


def test_per_noun_fix_format_paths_rewrites_with_bridge_project(alpha, monkeypatch):
    from urllib.parse import quote
    cap: list = []
    routes = {"/bridge/node/attach-formatter": (200, {"ok": True})}
    monkeypatch.setattr(core, "_bridge_http", _fake_bridge(routes, capture=cap))
    core.bridge_attach_formatter(alpha, "Alpha", "UI/MainWindow/L1", "Text",
                                 "Model/X/{#k}", "k=Model/X/K", fix_format_paths=True)
    _m, p = next(c for c in cap if "attach-formatter" in c[1])
    assert "format=" + quote("/Objects/Alpha/Model/X/{#k}", safe="") in p


def test_bridge_edit_fix_format_paths_rewrites_op(alpha, monkeypatch):
    seen: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(dict(_OK_REPORT), seen=seen))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_reported_project", lambda cfg, project: "Alpha")
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op) or {"ok": True})
    op = {**_AFMT, "format": "Model/X/{#k}", "sources": "k=Model/X/K",
          "fix_format_paths": True}
    out = core.bridge_edit(alpha, "Alpha", [op])
    assert out["report"]["errors"] == []
    assert applied[0]["format"] == "/Objects/Alpha/Model/X/{#k}"
    assert seen[0][1]["ops"][0]["format"] == "/Objects/Alpha/Model/X/{#k}"
    assert op["format"] == "Model/X/{#k}", "caller op must not be mutated"


# ---- C# bridge backstop: AttachFormatterInline (PRD 0023 #1) -----------------

def _attach_formatter_body() -> str:
    src = (Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs").read_text(
        encoding="utf-8-sig")
    body = src[src.index("private string AttachFormatterInline"):]
    return body[:body.index("\n    }\n")]


def test_bridge_attach_formatter_refuses_project_relative_before_any_write():
    """The format check must precede node resolution and every model mutation."""
    body = _attach_formatter_body()
    guard = body.index('"format_path_not_runtime_resolvable"')
    assert guard < body.index("ResolveNode(path)")
    assert guard < body.index("GetOrCreateVariable")
    assert guard < body.index("BuildFormattedLink(")
    assert "IsProjectRelativeFormatPath(format)" in body[:guard]
    # message names the problem and suggests the absolute form
    assert "not runtime resolvable" in body[guard:guard + 400]
    assert "/Objects/<Project>/" in body[guard:guard + 500]


def test_bridge_format_path_helper_matches_python_classifier():
    """The C# segment list and accepted prefixes mirror core.classify_format_path."""
    import re
    src = (Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs").read_text(
        encoding="utf-8-sig")
    arr = src[src.index("ProjectTopSegments = new[]"):]
    arr = arr[:arr.index("};")]
    assert set(re.findall(r'"(\w+)"', arr)) == set(core._PROJECT_TOP_SEGMENTS)
    helper = src[src.index("private static bool IsProjectRelativeFormatPath"):]
    helper = helper[:helper.index("\n    }\n")]
    for ok in ('"{#"', '"/Objects/"', '"../"', '"./"'):
        assert ok in helper


# ---- C# bridge: NodeId dynamic-link sources emit target_path (PRD 0023 #2) ---

def _bridge_src() -> str:
    return (Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs").read_text(
        encoding="utf-8-sig")


def _link_target_body() -> str:
    src = _bridge_src()
    body = src[src.index("private string LinkTargetJson"):]
    return body[:body.index("\n    }\n")]


def test_bridge_nodeid_link_emits_target_path_beside_raw_value():
    """A DynamicLink / Source* NodeId value is resolved with InformationModel.Get
    and serialized with the browse path from the project root (NodePathOf)."""
    body = _link_target_body()
    assert 'tn != "DynamicLink"' in body and 'StartsWith("Source")' in body
    assert "as NodeId" in body
    assert "InformationModel.Get(nid)" in body
    assert "NodePathOf(target)" in body
    assert '",\\"target_path\\":\\""' in body


def test_bridge_unresolvable_nodeid_yields_null_target_path_and_target_missing():
    body = _link_target_body()
    assert '",\\"target_path\\":null,\\"target_missing\\":true"' in body
    # a throwing Get() is treated as unresolvable, not as "not a link"
    assert "try { target = InformationModel.Get(nid); } catch { target = null; }" in body
    assert "if (target == null)" in body


def _nodeid_string_regex():
    """The C# TryParseNodeIdString grammar, lifted from the bridge source (verbatim
    string) so the unit test exercises the pattern the bridge really ships."""
    src = _bridge_src()
    m = re.search(r'NodeIdStringPattern =\s*@"((?:[^"]|"")*)";', src)
    assert m, "NodeIdStringPattern not found"
    return re.compile(m.group(1).replace('""', '"'))


def test_nodeid_string_parser_grammar():
    rx = _nodeid_string_regex()
    g = "0a1b2c3d-1111-2222-3333-444455556666"
    m = rx.match("{NodeId:ns=103;g=%s}" % g)
    assert m and m.group(1) == "103" and m.group(2) == g
    m = rx.match("  {NodeId:ns=2;i=4711}  ")
    assert m and m.group(1) == "2" and m.group(3) == "4711"
    m = rx.match("{NodeId:ns=2;s=Some.Tag}")
    assert m and m.group(4) == "Some.Tag"
    # browse paths / attribute refs / malformed forms are NOT NodeId strings
    for bad in ("Model/Var1", "/Objects/Proj/Model/Var1", "../../Alias1/MyInt",
                "{Alias1}/MyInt", "{NodeId:ns=x;g=abc}", "{NodeId:ns=1}", "",
                "{NodeId:ns=1;g=zz}"):
        assert not rx.match(bad), bad


def test_bridge_link_target_resolves_string_values_and_falls_back_to_path():
    body = _link_target_body()
    assert "TryParseNodeIdString(str, out parsed)" in body
    assert "ResolveNodeLoose(rel)" in body          # browse-path resolver
    assert "cand as string" in body
    src = _bridge_src()
    assert "private static bool TryParseNodeIdString(string s, out NodeId nid)" in src


def test_bridge_link_target_emitted_by_describe_property_and_map():
    """The fragment rides beside the raw value in every serializer that prints a
    variable value: describe (NodeJson), set_property echo (PropOkJson), map."""
    src = _bridge_src()
    for anchor in ("private string NodeJson(", "private string PropOkJson(",
                   "private void MapNodeJson("):
        seg = src[src.index(anchor):]
        seg = seg[:seg.index("\n    }\n")]
        assert "LinkTargetJson(" in seg, anchor


def test_formatter_with_two_sources_reports_target_path_for_each():
    """Each Source<i> variable is serialized independently by the NodeJson
    property loop, so a two-source formatter yields one target_path per source
    (live proof: test_bridge_live.py::test_formatter_two_sources_target_path_live)."""
    src = _bridge_src()
    seg = src[src.index("private string NodeJson("):]
    seg = seg[:seg.index("\n    }\n")]
    loop = seg[seg.index("foreach (var child in node.Children)"):]
    assert loop.count("LinkTargetJson(v)") == 1   # per-variable, inside the loop
    assert "foreach" in loop[:loop.index("LinkTargetJson(v)")]


# ---- vr7.5: validator prefix-class gap coverage + backstop wiring -----------

@pytest.mark.parametrize("seg", sorted(core._PROJECT_TOP_SEGMENTS))
def test_every_project_top_segment_is_project_relative(seg):
    """Each project-top segment, as a leading path component, is refused and
    gets the absolute rewrite."""
    fmt = f"{seg}/X/{{#k}}"
    assert core.classify_format_path(fmt) == "project_relative"
    assert core.format_path_rewrite(fmt, "P") == f"/Objects/P/{seg}/X/{{#k}}"


@pytest.mark.parametrize("fmt", [
    "Model",                    # bare segment name, no path separator
    "Models/X/{#k}",            # not a project-top segment
    "Foo/Model/{#k}",           # segment only counts when it is the first component
    "{#k}/Model/X",             # leading {# wins over a later project segment
    "/Objects/P/Model/{#k}",
    "../Model/{#k}",
    "./Model/{#k}",
])
def test_classify_format_path_does_not_over_refuse(fmt):
    assert core.classify_format_path(fmt) == "runtime_resolvable"


def test_fix_format_paths_leaves_runtime_resolvable_format_untouched(alpha, monkeypatch):
    seen: list = []
    monkeypatch.setattr(core, "_bridge_post_body", _fake_validate(dict(_OK_REPORT), seen=seen))
    monkeypatch.setattr(core, "_bridge_write_guard", lambda cfg, project: cfg)
    monkeypatch.setattr(core, "_bridge_reported_project", lambda cfg, project: "Alpha")
    applied: list = []
    monkeypatch.setattr(core, "_apply_one_edit",
                        lambda cfg, project, op: applied.append(op) or {"ok": True})
    op = {**_AFMT, "format": "../Panel{#k}@NodeId", "sources": "k=Model/X/K",
          "fix_format_paths": True}
    out = core.bridge_edit(alpha, "Alpha", [op])
    assert out["report"]["errors"] == []
    assert applied[0]["format"] == "../Panel{#k}@NodeId"


def test_bridge_attach_formatter_route_dispatches_to_guarded_inline():
    """The HTTP route for attach-formatter reaches AttachFormatterInline (the
    method holding the backstop), and the backstop is its first non-parse step."""
    src = _bridge_src()
    route = src[src.index('StartsWith("POST /bridge/node/attach-formatter")'):]
    assert "AttachFormatterInline(firstLine)" in route[:200]
    body = _attach_formatter_body()
    guard = body.index("IsProjectRelativeFormatPath(format)")
    assert body.index("QueryParam(firstLine, \"format\")") < guard
