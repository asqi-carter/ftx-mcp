"""Contract tests for the real dictionary node shape (PRD 0056, ftx-mcp-w3d.1).

The Studio 1.7.4 / 1.8.0 dictionary is ONE variable node (``LocalizationDictionary1``,
node_class Variable, datatype ``String[]``, value ``System.String[,]``), not an object
with a child ``Value`` variable. ``NodeShapeBridge`` is a fake bridge that models the
node tree and mirrors the C# ``ReadValueMatrix`` read order (node's own value first,
child ``Value`` only for non-variables), then answers the bridge routes from it.
The C# is not compiled here; the source pins check the read order and comments.
Pending bench verification: rebuild the bridge on 1.7 and 1.8, then run
``optix_bridge_add_translations`` with a de-DE row (dry_run) on an en-US-only project.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from service import core
from service.tests.conftest import make_project

_CS = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"
_DICT_PATH = "Translations/LocalizationDictionary1"
_MATRIX = [["", "en-US"]]          # header row: key column label + locales


class Node:
    """A model node: ``is_variable`` nodes carry their own ``value``; others may
    have a child variable named ``Value``."""

    def __init__(self, name, value=None, is_variable=False, value_child=None):
        self.name, self.value = name, value
        self.is_variable, self.value_child = is_variable, value_child


def read_value_matrix(nodes):
    """Python mirror of the C# ReadValueMatrix: own value, else child ``Value``."""
    for n in nodes:
        if n.is_variable:
            raw = n.value
        elif n.value_child is not None:
            raw = n.value_child
        else:
            continue
        if isinstance(raw, list) and raw and all(isinstance(r, list) for r in raw):
            return raw
    return None


class NodeShapeBridge:
    def __init__(self, nodes):
        self.nodes = nodes
        self.writes = []

    def matrix(self):
        return read_value_matrix(self.nodes)

    def locales(self):
        m = self.matrix()
        return None if m is None else [c for c in m[0][1:] if c]

    def keys_locales(self, key):
        m = self.matrix() or []
        out = []
        for row in m[1:]:
            if row and row[0] == key:
                out += [m[0][j] for j in range(1, len(row)) if row[j]]
        for k, loc, _v in self.writes:
            if k == key and loc not in out:
                out.append(loc)
        return out

    def refuse(self, rows):
        avail = self.locales()
        if avail is None:
            return {"code": "locale_check_unavailable", "op_index": 0,
                    "dictionary_path": _DICT_PATH,
                    "message": f"cannot verify locales: no locale list in '{_DICT_PATH}'"}
        for r, row in enumerate(rows):
            if row["locale"] not in avail:
                return {"code": "unknown_locale", "op_index": 0, "row": r,
                        "locale": row["locale"], "available_locales": avail,
                        "message": f"rows[{r}]: unknown locale {row['locale']!r}"}
        return None

    def http(self, cfg, path, method="GET", timeout=5.0, **_kw):
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "0.5.0-phase1-materialize",
                                    "project": "Alpha", "model_loaded": True}).encode()
        q = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
        if path.startswith("/bridge/node/text-key"):
            locs = self.keys_locales(q["key"])
            return 200, json.dumps({"ok": True, "key": q["key"], "text_id": q["key"],
                                    "translated": bool(locs),
                                    "translated_locales": locs}).encode()
        return 404, b'{"error":{"code":"not_found"}}'

    def post_body(self, cfg, path, payload, timeout=20.0):
        rows, dry = payload["rows"], "dry_run=true" in path
        bad = self.refuse(rows)
        if bad:
            return 200, {"error": bad}
        if not dry:
            self.writes += [(r["key"], r["locale"], r["value"]) for r in rows]
        return 200, {"ok": True, "dry_run": dry, "count": len(rows),
                     "rows": [{"key": r["key"], "new": True} for r in rows]}

    def validate_ops(self, cfg, project, ops, strict=False):
        errors = []
        for i, op in enumerate(ops):
            if op.get("op") == "add_translations":
                bad = self.refuse([{"locale": r.get("locale", "en-US")} for r in op["rows"]])
                if bad:
                    errors.append({**bad, "op_index": i})
        return {"ok": not errors, "errors": errors, "warnings": [],
                "op_count": len(ops), "strict": strict}


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    core.reset_bridge_cache()
    yield cfg
    core.reset_bridge_cache()


def _install(monkeypatch, b):
    monkeypatch.setattr(core, "_bridge_http", b.http)
    monkeypatch.setattr(core, "_bridge_post_body", b.post_body)
    monkeypatch.setattr(core, "bridge_validate_ops", b.validate_ops)
    monkeypatch.setattr(core, "validate_edit_op_types", lambda *a, **k: [])


def _real_shape(matrix=None):
    """Translations folder + LocalizationDictionary1 variable holding the matrix."""
    return NodeShapeBridge([
        Node("Translations"),
        Node("LocalizationDictionary1", value=[list(r) for r in (matrix or _MATRIX)],
             is_variable=True)])


ROW_EN = {"key": "greeting", "value": "Hello", "locale": "en-US"}
ROW_DE = {"key": "greeting", "value": "Hallo", "locale": "de-DE"}


# -- shape 1: en-US rows are written ----------------------------------------------

def test_add_translations_en_us_rows_written_against_own_value_matrix(alpha, monkeypatch):
    b = _real_shape()
    _install(monkeypatch, b)
    out = core.bridge_add_translations(alpha, "Alpha", [ROW_EN], dry_run=False)
    assert out["ok"] is True
    assert b.writes == [("greeting", "en-US", "Hello")]


# -- shape 2: de-DE refused, dry_run and apply ------------------------------------

@pytest.mark.parametrize("dry_run", [True, False])
def test_add_translations_de_de_refused_as_unknown_locale_with_available_en_us(
        alpha, monkeypatch, dry_run):
    b = _real_shape()
    _install(monkeypatch, b)
    out = core.bridge_add_translations(alpha, "Alpha", [ROW_DE], dry_run=dry_run)
    assert out["ok"] is False and out["code"] == "unknown_locale"
    assert out["locale"] == "de-DE" and out["available_locales"] == ["en-US"]
    assert b.writes == []


def test_batch_edit_refuses_de_de_as_unknown_locale(alpha, monkeypatch):
    b = _real_shape()
    _install(monkeypatch, b)
    out = core.bridge_edit(alpha, "Alpha", [{"op": "add_translations", "rows": [ROW_DE]}])
    assert out["applied"] == 0 and b.writes == []
    err = next(e for e in out["report"]["errors"] if e["code"] == "unknown_locale")
    assert err["available_locales"] == ["en-US"]


# -- shape 3: locale_check_unavailable only without a readable matrix ---------------

@pytest.mark.parametrize("nodes", [
    [],                                                              # no dictionary node
    [Node("Translations")],                                          # folder only
    [Node("Translations"), Node("LocalizationDictionary1", value="not a matrix",
                                is_variable=True)],                  # value not a matrix
    [Node("Translations"), Node("LocalizationDictionary1", value=["a", "b"],
                                is_variable=True)],                  # 1-D array
], ids=["no_node", "folder_only", "scalar_value", "flat_array"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_locale_check_unavailable_only_when_no_dictionary_or_not_a_matrix(
        alpha, monkeypatch, nodes, dry_run):
    b = NodeShapeBridge(nodes)
    _install(monkeypatch, b)
    out = core.bridge_add_translations(alpha, "Alpha", [ROW_EN], dry_run=dry_run)
    assert out["ok"] is False and out["code"] == "locale_check_unavailable"
    assert out["dictionary_path"] == _DICT_PATH and b.writes == []


def test_matrix_on_the_nodes_own_value_is_readable_without_a_value_child(alpha, monkeypatch):
    # The regression: the old reader looked for a child `Value` variable only.
    b = _real_shape()
    assert b.nodes[1].value_child is None and b.locales() == ["en-US"]
    _install(monkeypatch, b)
    assert core.bridge_add_translations(alpha, "Alpha", [ROW_EN], dry_run=True)["ok"] is True


def test_child_value_variable_still_read_for_non_variable_nodes():
    b = NodeShapeBridge([Node("Translations"),
                         Node("Dict", value_child=[["", "en-US", "de-DE"]])])
    assert b.locales() == ["en-US", "de-DE"]


# -- set_text_key ------------------------------------------------------------------

def test_set_text_key_on_key_in_matrix_is_translated_true_with_locales(alpha, monkeypatch):
    b = _real_shape([["", "en-US", "de-DE"], ["greeting", "Hello", "Hallo"]])
    _install(monkeypatch, b)
    out = core.bridge_set_text_key(alpha, "Alpha", "UI/W/L1", "Text", "greeting")
    assert out["translated"] is True
    assert out["translated_locales"] == ["en-US", "de-DE"]


def test_set_text_key_on_key_absent_from_matrix_is_translated_false(alpha, monkeypatch):
    b = _real_shape([["", "en-US"], ["other", "x"]])
    _install(monkeypatch, b)
    out = core.bridge_set_text_key(alpha, "Alpha", "UI/W/L1", "Text", "greeting")
    assert out["translated"] is False and out["translated_locales"] == []


# -- C# source pins ------------------------------------------------------------------

def _cs_between(start, end):
    src = _CS.read_text(encoding="utf-8")
    return src[src.index(start):src.index(end)]


def test_cs_read_value_matrix_reads_own_variable_value_before_child_value():
    body = _cs_between("private static string[][] ReadValueMatrix",
                       "// Locale ids from the matrix header")
    assert "n as IUAVariable" in body and "uv.Value.Value" in body
    assert body.index("uv.Value.Value") < body.index('GetVariable("Value")')
    assert "else" in body[body.index("uv.Value.Value"):body.index('GetVariable("Value")')]


def test_cs_documents_real_node_shape_and_labels_locales_variables_unobserved():
    doc = _cs_between("// ---- translation dictionary layout (1.7 vs 1.8)", "private static string CellText")
    for s in ("LocalizationDictionary1", "Variable", "String[]", "System.String[,]"):
        assert s in doc
    fb = _cs_between("private static System.Collections.Generic.List<string> DictionaryLocales(out string path)",
                     "private static bool LocaleKnown(")
    assert "unobserved; the matrix variable is the known shape on 1.7.4 and 1.8.0" in fb
