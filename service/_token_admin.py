"""Token-table admin CLI for the issue/revoke PowerShell wrappers.

Pure JSON in, JSON out. The PS1 scripts handle DPAPI wrap/unwrap on disk
and pipe the decrypted payload (or empty bytes for a fresh install) to
this module. Keeping the table-mutation logic in Python lets us test the
add/remove/list contract under pytest on Linux without touching DPAPI.

Subcommands:

  add     Append a new token. stdin = current decrypted JSON (or empty).
          stdout = JSON {"payload": <new-decrypted-json>, "bearer": <secret>,
                         "id": <ulid>}. The bearer secret is printed exactly
                         once; the caller (issue-token.ps1) is responsible
                         for surfacing it to the operator.

  remove  Drop a token by --id. stdin = current decrypted JSON.
          stdout = JSON {"payload": <new-decrypted-json>, "removed": <id>}.
          Exits 2 if --id not found (caller should print a friendly error).

  list    Read-only inspection. stdin = current decrypted JSON.
          stdout = JSON {"tokens": [{"id","label","scope","created_at",
                                     "expires_at","last_seen_at"}, ...]}.
          The hash field is intentionally omitted — there is no operator
          workflow that needs it, and printing hashes invites bad habits.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import auth


def _empty_payload() -> dict[str, Any]:
    return {"tokens": []}


def _read_payload(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        return _empty_payload()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "tokens" not in payload:
        raise ValueError("malformed token payload: top-level 'tokens' missing")
    return payload


def cmd_add(args: argparse.Namespace) -> int:
    payload = _read_payload(sys.stdin.read())
    token_id, bearer = auth.generate_token()
    record = auth.TokenRecord(
        id=token_id,
        label=args.label,
        scope=args.scope,
        hash=auth.hash_secret(bearer),
        created_at=auth.now_iso(),
        last_seen_at=None,
        expires_at=args.expires_at,
    )
    if any(r.get("id") == token_id for r in payload["tokens"]):
        # ULID collision is statistically negligible; if it fires the
        # caller should retry rather than mutate state, so fail loud.
        print(f"ERR: id collision on {token_id}", file=sys.stderr)
        return 1
    payload["tokens"].append(auth.serialize_record(record))
    json.dump(
        {
            "payload": payload,
            "bearer": bearer,
            "id": token_id,
            "label": args.label,
            "scope": args.scope,
        },
        sys.stdout,
    )
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    payload = _read_payload(sys.stdin.read())
    before = len(payload["tokens"])
    payload["tokens"] = [r for r in payload["tokens"] if r.get("id") != args.id]
    if len(payload["tokens"]) == before:
        print(f"ERR: no token with id {args.id}", file=sys.stderr)
        return 2
    json.dump({"payload": payload, "removed": args.id}, sys.stdout)
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    payload = _read_payload(sys.stdin.read())
    visible = [
        {
            "id": r.get("id"),
            "label": r.get("label"),
            "scope": r.get("scope"),
            "created_at": r.get("created_at"),
            "expires_at": r.get("expires_at"),
            "last_seen_at": r.get("last_seen_at"),
        }
        for r in payload["tokens"]
    ]
    json.dump({"tokens": visible}, sys.stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m service._token_admin",
        description="Token-table admin CLI (add/remove/list). Pure JSON I/O; "
                    "DPAPI wrap/unwrap is the PowerShell wrapper's job.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="add a new token")
    p_add.add_argument("--label", required=True, help="human-readable label")
    p_add.add_argument(
        "--scope", required=True, choices=("health", "read", "deploy"),
        help="token scope (health ⊆ read ⊆ deploy)",
    )
    p_add.add_argument(
        "--expires-at", default=None,
        help="ISO8601-Z expiry, or omit for never",
    )
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", help="remove a token by id")
    p_remove.add_argument("--id", required=True, help="token id (ULID hex)")
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="list tokens (no hash exposed)")
    p_list.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except json.JSONDecodeError as exc:
        print(f"ERR: stdin is not valid JSON: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        # Malformed payload (missing 'tokens' key, etc.) — keep messages
        # friendly so the PS1 wrapper can echo them straight through.
        print(f"ERR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
