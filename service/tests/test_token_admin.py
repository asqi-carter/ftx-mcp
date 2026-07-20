"""Tests for service._token_admin — the JSON-in/JSON-out CLI invoked by
issue-token.ps1 / revoke-token.ps1.

The PS1 wrappers handle DPAPI; this module handles the table mutation
contract. Linux-runner-friendly.
"""
from __future__ import annotations

import io
import json

import pytest

from service import _token_admin, auth


def _run(argv: list[str], stdin: str = "") -> tuple[int, str, str]:
    """Drive the CLI with a piped stdin/stdout/stderr. Returns
    (exit_code, stdout, stderr) so callers can assert on each."""
    sin, sout, serr = io.StringIO(stdin), io.StringIO(), io.StringIO()
    import sys as _sys
    real_in, real_out, real_err = _sys.stdin, _sys.stdout, _sys.stderr
    _sys.stdin, _sys.stdout, _sys.stderr = sin, sout, serr
    try:
        rc = _token_admin.main(argv)
    finally:
        _sys.stdin, _sys.stdout, _sys.stderr = real_in, real_out, real_err
    return rc, sout.getvalue(), serr.getvalue()


class TestAdd:
    def test_add_to_empty_payload_creates_first_token(self) -> None:
        rc, out, _err = _run(["add", "--label", "first", "--scope", "deploy"], stdin="")
        assert rc == 0
        result = json.loads(out)
        assert result["label"] == "first"
        assert result["scope"] == "deploy"
        assert result["bearer"].startswith("ftxm_")
        assert result["id"]
        assert len(result["payload"]["tokens"]) == 1
        rec = result["payload"]["tokens"][0]
        # The bearer's hash MUST be what's persisted, not the bearer itself.
        assert rec["hash"] == auth.hash_secret(result["bearer"])
        assert "bearer" not in rec  # never persist the secret

    def test_add_appends_to_existing_payload(self) -> None:
        rec_a, _ = self._make_existing_record(label="laptop")
        existing = json.dumps({"tokens": [auth.serialize_record(rec_a)]})
        rc, out, _err = _run(["add", "--label", "workstation", "--scope", "read"], stdin=existing)
        assert rc == 0
        result = json.loads(out)
        labels = sorted(t["label"] for t in result["payload"]["tokens"])
        assert labels == ["laptop", "workstation"]

    def test_add_with_expires_at_persists(self) -> None:
        rc, out, _err = _run(
            ["add", "--label", "shortlived", "--scope", "health",
             "--expires-at", "2030-01-01T00:00:00Z"],
            stdin="",
        )
        assert rc == 0
        result = json.loads(out)
        assert result["payload"]["tokens"][0]["expires_at"] == "2030-01-01T00:00:00Z"

    def test_add_rejects_invalid_scope(self) -> None:
        with pytest.raises(SystemExit):  # argparse choices reject 'admin'
            _run(["add", "--label", "x", "--scope", "admin"], stdin="")

    def test_add_rejects_malformed_payload(self) -> None:
        rc, _out, err = _run(["add", "--label", "x", "--scope", "read"], stdin="not-json")
        assert rc != 0
        # The error message comes from json.JSONDecodeError, surfaced via SystemExit
        # — pytest captures it through stderr/exception path; either way rc != 0.

    @staticmethod
    def _make_existing_record(label: str) -> tuple[auth.TokenRecord, str]:
        token_id, bearer = auth.generate_token()
        return auth.TokenRecord(
            id=token_id,
            label=label,
            scope="deploy",
            hash=auth.hash_secret(bearer),
            created_at=auth.now_iso(),
        ), bearer


class TestRemove:
    def test_remove_known_id(self) -> None:
        rec, _ = self._make_record()
        existing = json.dumps({"tokens": [auth.serialize_record(rec)]})
        rc, out, _err = _run(["remove", "--id", rec.id], stdin=existing)
        assert rc == 0
        result = json.loads(out)
        assert result["removed"] == rec.id
        assert result["payload"]["tokens"] == []

    def test_remove_unknown_id_exits_2(self) -> None:
        rec, _ = self._make_record()
        existing = json.dumps({"tokens": [auth.serialize_record(rec)]})
        rc, _out, err = _run(["remove", "--id", "deadbeef"], stdin=existing)
        assert rc == 2
        assert "deadbeef" in err

    def test_remove_only_drops_matching_id(self) -> None:
        rec_a, _ = self._make_record(label="a")
        rec_b, _ = self._make_record(label="b")
        existing = json.dumps({
            "tokens": [auth.serialize_record(rec_a), auth.serialize_record(rec_b)],
        })
        rc, out, _err = _run(["remove", "--id", rec_a.id], stdin=existing)
        assert rc == 0
        result = json.loads(out)
        assert [t["id"] for t in result["payload"]["tokens"]] == [rec_b.id]

    @staticmethod
    def _make_record(label: str = "x") -> tuple[auth.TokenRecord, str]:
        token_id, bearer = auth.generate_token()
        return auth.TokenRecord(
            id=token_id,
            label=label,
            scope="deploy",
            hash=auth.hash_secret(bearer),
            created_at=auth.now_iso(),
        ), bearer


class TestList:
    def test_list_omits_hash(self) -> None:
        rec, _ = TestRemove._make_record(label="visible")
        existing = json.dumps({"tokens": [auth.serialize_record(rec)]})
        rc, out, _err = _run(["list"], stdin=existing)
        assert rc == 0
        result = json.loads(out)
        assert len(result["tokens"]) == 1
        entry = result["tokens"][0]
        assert "hash" not in entry
        assert entry["label"] == "visible"
        assert entry["scope"] == "deploy"
        assert entry["id"] == rec.id

    def test_list_empty_payload(self) -> None:
        rc, out, _err = _run(["list"], stdin="")
        assert rc == 0
        assert json.loads(out) == {"tokens": []}
