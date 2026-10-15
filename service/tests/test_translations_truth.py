"""Truthful ``translated`` and atomic ``add_translations`` (PRD 0052 C, #83).

Offline: a ``FakeDictionary`` stands in for the bridge's translation dictionary and
implements the bridge's documented contract (one existence check shared by
add_translation and set_text_key; locale pre-check before any write; a failed
write reports ``partial``/``written``). It has ONLY the ``en-US`` locale column.
The C# itself is not compiled here; ``test_cs_*`` pin the source-level ordering.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from service import core
from service.tests.conftest import make_project

_CS = Path(__file__).resolve().parents[2] / "studio-bridge" / "StudioMCPBridge.cs"


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    core.reset_bridge_cache()
    yield
    core.reset_bridge_cache()


@pytest.fixture
def alpha(cfg, projects_root):
    make_project(projects_root, "Alpha")
    return cfg


class FakeDictionary:
    """A translation dictionary with fixed locale columns (default: en-US only)."""

    def __init__(self, locales=("en-US",), fail_on_key: str | None = None):
        self.locales = None if locales is None else list(locales)
        self.rows: dict[str, dict[str, str]] = {}      # key -> locale -> text
        self.fail_on_key = fail_on_key
        self.writes: list[tuple[str, str, str]] = []

    # the ONE existence check (add_translation and set_text_key both use it)
    def locales_for(self, key: str) -> list[str]:
        return list(self.rows.get(key, {}))

    def unknown_locale(self, rows, op_index=0):
        if self.locales is None:        # no locale list readable: refuse, never skip
            return {"code": "locale_check_unavailable", "op_index": op_index,
                    "dictionary_path": "Translations/LocalizationDictionary1",
                    "message": "cannot verify locales: no locale list could be read "
                               "from the translation dictionary "
                               "'Translations/LocalizationDictionary1'"}
        for r, row in enumerate(rows):
            if row["locale"] not in self.locales:
                return {"code": "unknown_locale", "op_index": op_index, "row": r,
                        "locale": row["locale"], "available_locales": list(self.locales),
                        "message": f"rows[{r}]: locale {row['locale']!r} is not a locale "
                                   f"of the translation dictionary"}
        return None

    def write(self, key, value, locale) -> bool:
        if key == self.fail_on_key:
            raise RuntimeError("SDK refused the row")
        is_new = key not in self.rows
        self.rows.setdefault(key, {})[locale] = value
        self.writes.append((key, locale, value))
        return is_new

    # ---- the three bridge routes, as the wire sees them
    def http(self, cfg, path, method="GET", timeout=5.0, **_kw):
        if path.startswith("/bridge/health"):
            return 200, json.dumps({"bridge_version": "0.5.0-phase1-materialize",
                                    "project": "Alpha", "model_loaded": True}).encode()
        q = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
        if path.startswith("/bridge/i18n/translation?"):
            new = self.write(q["key"], q.get("value", ""), q.get("locale", "en-US"))
            return 200, json.dumps({"ok": True, "key": q["key"], "new": new}).encode()
        if path.startswith("/bridge/node/text-key"):
            locales = self.locales_for(q["key"])
            return 200, json.dumps({"ok": True, "key": q["key"], "text_id": q["key"],
                                    "translated": bool(locales),
                                    "translated_locales": locales}).encode()
        return 404, b'{"error":{"code":"not_found"}}'

    def post_body(self, cfg, path, payload, timeout=20.0):
        rows = payload["rows"]
        dry = "dry_run=true" in path
        bad = self.unknown_locale(rows)
        if bad:
            return 200, {"error": bad}
        written = 0
        report = []
        for r, row in enumerate(rows):
            if dry:
                report.append({"key": row["key"], "new": row["key"] not in self.rows})
                continue
            try:
                new = self.write(row["key"], row["value"], row["locale"])
            except RuntimeError as exc:
                return 200, {"ok": False, "partial": written > 0, "written": written,
                             "error": {"code": "row_failed", "row": r, "partial": written > 0,
                                       "written": written, "message": str(exc)}}
            written += 1
            report.append({"key": row["key"], "new": new})
        return 200, {"ok": True, "dry_run": dry, "count": len(rows), "rows": report}

    def validate_ops(self, cfg, project, ops, strict=False):
        errors = []
        for i, op in enumerate(ops):
            if op.get("op") == "add_translations":
                rows = [{"locale": r.get("locale") or op.get("locale", "en-US")}
                        for r in op["rows"]]
                bad = self.unknown_locale(rows, op_index=i)
                if bad:
                    errors.append(bad)
        return {"ok": not errors, "errors": errors, "warnings": [],
                "op_count": len(ops), "strict": strict}


@pytest.fixture
def fake(alpha, monkeypatch):
    d = FakeDictionary()
    monkeypatch.setattr(core, "_bridge_http", d.http)
    monkeypatch.setattr(core, "_bridge_post_body", d.post_body)
    monkeypatch.setattr(core, "bridge_validate_ops", d.validate_ops)
    monkeypatch.setattr(core, "validate_edit_op_types", lambda *a, **k: [])
    return d


ROWS = [{"key": "a", "value": "A", "locale": "en-US"},
        {"key": "b", "value": "Bee", "locale": "de-DE"}]


# -- translated flag ------------------------------------------------------------

def test_set_text_key_returns_translated_true_after_add_translation(alpha, fake):
    before = core.bridge_set_text_key(alpha, "Alpha", "UI/W/L1", "Text", "greeting")
    assert before["translated"] is False and before["translated_locales"] == []
    core.bridge_add_translation(alpha, "Alpha", "greeting", "Hello", "en-US")
    after = core.bridge_set_text_key(alpha, "Alpha", "UI/W/L1", "Text", "greeting")
    assert after["translated"] is True
    assert after["translated_locales"] == ["en-US"]


def test_cs_set_text_key_uses_the_add_translation_existence_check():
    src = _CS.read_text(encoding="utf-8")
    body = src[src.index("private string SetTextKeyInline"):src.index("// ---- set_modelling_rule")]
    assert "TranslationLocalesFor(key)" in body and "TranslationRowExists(key)" in body
    assert "translated_locales" in body
    # the old always-false lookup (bare LocalizedText(key) probe inline) is gone
    assert "InformationModel.LookupTranslation(new LocalizedText(key))" not in body
    upsert = src[src.index("private static bool UpsertTranslation"):src.index("// Structural check of one add_translations row")]
    assert "TranslationRowExists(key)" in upsert      # same check decides new vs update


# -- unknown locale -------------------------------------------------------------

@pytest.mark.parametrize("dry_run", [True, False])
def test_add_translations_unknown_locale_refused_at_dry_run_and_apply(alpha, fake, dry_run):
    out = core.bridge_add_translations(alpha, "Alpha", ROWS, dry_run=dry_run)
    assert out["ok"] is False and out["code"] == "unknown_locale"
    assert out["row"] == 1 and out["op_index"] == 0
    assert out["locale"] == "de-DE" and out["available_locales"] == ["en-US"]
    assert out["written"] == 0
    assert fake.writes == []


def test_add_translations_no_rows_written_when_any_locale_unknown(alpha, fake):
    out = core.bridge_add_translations(alpha, "Alpha", ROWS)
    assert out["code"] == "unknown_locale"
    assert fake.rows == {}                       # the valid en-US row 0 was NOT written


def test_add_translations_known_locale_still_applies(alpha, fake):
    out = core.bridge_add_translations(alpha, "Alpha", ROWS[:1])
    assert out["ok"] is True and fake.rows == {"a": {"en-US": "A"}}


def test_batch_dry_run_refuses_unknown_locale_with_op_index_row_and_locales(alpha, fake):
    ops = [{"op": "add_translations", "rows": [{"key": "k", "value": "v"}]},
           {"op": "add_translations", "rows": ROWS}]
    out = core.bridge_edit(alpha, "Alpha", ops, dry_run=True)
    err = next(e for e in out["report"]["errors"] if e["code"] == "unknown_locale")
    assert err["op_index"] == 1 and err["row"] == 1
    assert err["available_locales"] == ["en-US"]
    assert out["state"] == "validated" and out["report"]["ok"] is False
    assert fake.writes == []


def test_batch_apply_refuses_unknown_locale_before_writing(alpha, fake):
    ops = [{"op": "add_translations", "rows": ROWS}]
    out = core.bridge_edit(alpha, "Alpha", ops)
    assert out["applied"] == 0 and fake.writes == []
    assert out["state"] == "validated"           # refused at validation, never applied
    assert any(e["code"] == "unknown_locale" for e in out["report"]["errors"])


# -- mid-way failure ------------------------------------------------------------

def test_add_translations_midway_failure_reports_partial_not_unchanged(alpha, monkeypatch):
    d = FakeDictionary(fail_on_key="c")
    monkeypatch.setattr(core, "_bridge_http", d.http)
    monkeypatch.setattr(core, "_bridge_post_body", d.post_body)
    monkeypatch.setattr(core, "bridge_validate_ops", d.validate_ops)
    monkeypatch.setattr(core, "validate_edit_op_types", lambda *a, **k: [])
    rows = [{"key": k, "value": k.upper(), "locale": "en-US"} for k in ("a", "b", "c", "d")]

    direct = core.bridge_add_translations(alpha, "Alpha", rows)
    assert direct["ok"] is False and direct["code"] == "row_failed"
    assert direct["partial"] is True and direct["written"] == 2 and direct["row"] == 2

    # as the FIRST (and only) op of a batch: applied == 0, yet the state is partial
    out = core.bridge_edit(alpha, "Alpha", [{"op": "add_translations", "rows": rows}])
    assert out["applied"] == 0
    assert out["state"] == "partial"
    assert "unchanged" not in out["nudge"]
    assert out["failed_op"]["partial"] is True and out["failed_op"]["written"] == 2


def test_add_translations_failure_on_first_row_is_still_unchanged_failed(alpha, monkeypatch):
    d = FakeDictionary(fail_on_key="a")
    monkeypatch.setattr(core, "_bridge_http", d.http)
    monkeypatch.setattr(core, "_bridge_post_body", d.post_body)
    monkeypatch.setattr(core, "bridge_validate_ops", d.validate_ops)
    monkeypatch.setattr(core, "validate_edit_op_types", lambda *a, **k: [])
    out = core.bridge_edit(alpha, "Alpha", [
        {"op": "add_translations", "rows": [{"key": "a", "value": "A"}]}])
    assert out["state"] == "failed" and "unchanged" in out["nudge"]


def test_apply_failure_outcome_partial_written_never_unchanged():
    state, nudge = core._apply_failure_outcome(0, 0, 3)
    assert state == "partial" and "unchanged" not in nudge
    assert core._apply_failure_outcome(0, 0)[0] == "failed"
    assert core._apply_failure_outcome(2, 2)[0] == "partial"


def test_cs_add_translations_checks_locales_before_the_first_write():
    src = _CS.read_text(encoding="utf-8")
    body = src[src.index("private string AddTranslationsInline"):src.index("// Shared refusal text for remove_translation")]
    assert body.index("UnknownLocaleJson(") < body.index("UpsertTranslation(")
    assert re.search(r'"partial\\":', body) and "row_failed" in body
    validate = src[src.index('case "add_translations":'):src.index('case "remove_translation":')]
    assert "unknown_locale" in validate and "available_locales" in validate


# -- locale_check_unavailable (PRD 0054 B) ---------------------------------------

def _install(monkeypatch, d):
    monkeypatch.setattr(core, "_bridge_http", d.http)
    monkeypatch.setattr(core, "_bridge_post_body", d.post_body)
    monkeypatch.setattr(core, "bridge_validate_ops", d.validate_ops)
    monkeypatch.setattr(core, "validate_edit_op_types", lambda *a, **k: [])


@pytest.mark.parametrize("dry_run", [True, False])
def test_add_translations_locale_check_unavailable_refused_with_dictionary_path(
        alpha, monkeypatch, dry_run):
    d = FakeDictionary(locales=None)
    _install(monkeypatch, d)
    out = core.bridge_add_translations(alpha, "Alpha", ROWS[:1], dry_run=dry_run)
    assert out["ok"] is False and out["code"] == "locale_check_unavailable"
    assert out["dictionary_path"] == "Translations/LocalizationDictionary1"
    assert out["written"] == 0 and out["dry_run"] is dry_run
    assert "Translations/LocalizationDictionary1" in out["message"]
    assert d.writes == []                      # the check is refused, not skipped


def test_batch_refuses_locale_check_unavailable_before_writing(alpha, monkeypatch):
    d = FakeDictionary(locales=None)
    _install(monkeypatch, d)
    out = core.bridge_edit(alpha, "Alpha", [{"op": "add_translations", "rows": ROWS[:1]}])
    assert out["applied"] == 0 and d.writes == []
    assert out["state"] == "validated"
    err = next(e for e in out["report"]["errors"] if e["code"] == "locale_check_unavailable")
    assert err["dictionary_path"] == "Translations/LocalizationDictionary1"


def test_add_translations_de_de_against_en_us_only_is_unknown_locale_in_dry_run(alpha, fake):
    out = core.bridge_add_translations(alpha, "Alpha", [ROWS[1]], dry_run=True)
    assert out["code"] == "unknown_locale" and out["locale"] == "de-DE"
    assert out["available_locales"] == ["en-US"] and fake.writes == []


# -- C# source pins: matrix header + matrix scan (not compiled here) ----------------

def _cs_between(start: str, end: str) -> str:
    src = _CS.read_text(encoding="utf-8")
    return src[src.index(start):src.index(end)]


def test_cs_dictionary_locales_reads_matrix_header_when_variables_absent():
    body = _cs_between("private static System.Collections.Generic.List<string> DictionaryLocales(out string path)",
                       "private static bool LocaleKnown(")
    assert body.index('"Locales"') < body.index("MatrixHeaderLocales(")   # old path first
    assert '"LocaleIds"' in body
    hdr = _cs_between("private static System.Collections.Generic.List<string> MatrixHeaderLocales",
                      "// Matrix scan: key in column 0")
    assert "j = 1" in hdr and "m[0]" in hdr                              # row 0, columns 1..n


def test_cs_matrix_layout_and_version_difference_are_documented():
    doc = _cs_between("// ---- translation dictionary layout (1.7 vs 1.8)", "private static string CellText")
    assert "1.8" in doc and "1.7" in doc and "Value" in doc
    assert "Row 0" in doc and "column 0" in doc and "Locales" in doc


def test_cs_translated_lookup_scans_matrix_key_column_0_locale_column_j():
    scan = _cs_between("// Matrix scan: key in column 0", "// True when some matrix row")
    assert "m[i][0]" in scan and "m[i][col]" in scan and "m[0][j]" in scan
    lookup = _cs_between("private static string TranslationLookupText", "private static bool TranslationLookupHit")
    assert "MatrixText(key, locale)" in lookup
    exists = _cs_between("private static bool TranslationRowExists", "private static void NoteTranslationWritten")
    assert "MatrixHasKey(key)" in exists


def test_cs_unreadable_locale_list_refuses_instead_of_skipping():
    unk = _cs_between("private static string UnknownLocaleJson", "// Add the row if the key is new")
    assert "locale_check_unavailable" in unk and "dictionary_path" in unk
    assert "if (avail == null) return null;" not in unk
    validate = _cs_between('case "add_translations":', 'case "remove_translation":')
    assert "locale_check_unavailable" in validate and "txAvail != null &&" not in validate
