"""attach_* `sources` handling and the attach_string_formatter placeholder guard.

Live finding (2026-09-24): an agent passed `source`
(singular) to attach_string_formatter. It was only an unknown_op_field
warning, so it was dropped; the formatter attached with no Source0 and the
batch reported `succeeded`. Passing `sources` as a JSON list was stringified
to "['Model/X']" and failed source_not_variable.
"""
from __future__ import annotations

import json
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
