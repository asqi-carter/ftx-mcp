"""Bearer-token auth for ftx-mcp HTTP and MCP surfaces.

One access-control policy in the service
layer; HTTP and MCP are thin wrappers above it. The middleware here is
plain ASGI so the same callable can wrap both `FastAPI` and the Starlette
app returned by `FastMCP.streamable_http_app()`.

Trust boundary: the on-disk token table holds sha256 hashes of secrets,
not the secrets themselves. The bearer string is shown once at issue
time and never persisted. DPAPI-encrypted-at-rest is layered on top via
`tokens.json.dpapi`: when the configured path ends in `.dpapi`,
`TokenStore` reads the ciphertext and round-trips it through `_dpapi`
(ctypes wrapper around `CryptUnprotectData`). Plaintext `tokens.json` is
still accepted for Linux dev/test runs where DPAPI is not available.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _dpapi

# ---- scope model ------------------------------------------------------

# Order matters: index encodes the ⊆ relation. health ⊆ read ⊆ deploy.
_SCOPE_ORDER: tuple[str, ...] = ("health", "read", "deploy")

SCOPES: frozenset[str] = frozenset(_SCOPE_ORDER)


def scope_satisfies(token_scope: str, required: str) -> bool:
    """Return True iff a token labelled `token_scope` covers `required`.

    Raises ValueError on unknown scope labels (caller bug, not auth fail).
    """
    if token_scope not in SCOPES:
        raise ValueError(f"unknown token scope: {token_scope!r}")
    if required not in SCOPES:
        raise ValueError(f"unknown required scope: {required!r}")
    return _SCOPE_ORDER.index(token_scope) >= _SCOPE_ORDER.index(required)


# ---- token generation + hashing --------------------------------------

_TOKEN_PREFIX = "ftxm_"
_TOKEN_SECRET_BYTES = 32


def generate_token() -> tuple[str, str]:
    """Return (token_id, bearer_secret).

    `bearer_secret` is `ftxm_<base64url(32 bytes)>` — recognizable in logs
    by prefix without revealing the secret. Caller MUST surface
    bearer_secret exactly once and never persist it; only the sha256
    hash should land in `tokens.json`.
    """
    token_id = uuid.uuid4().hex
    raw = secrets.token_bytes(_TOKEN_SECRET_BYTES)
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return token_id, _TOKEN_PREFIX + body


def hash_secret(secret: str) -> str:
    """Return `sha256:<hex>` of the bearer secret. Constant prefix so the
    storage format can pivot to argon2/etc later without ambiguity."""
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _verify_secret(presented: str, stored_hash: str) -> bool:
    """Constant-time comparison. Defends against timing oracles on the
    hash table lookup. The hash itself is not secret, but consistent
    timing makes the reasoning simpler."""
    return secrets.compare_digest(hash_secret(presented), stored_hash)


# ---- token table -----------------------------------------------------

@dataclass(frozen=True)
class TokenRecord:
    id: str
    label: str
    scope: str
    hash: str
    created_at: str
    last_seen_at: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> TokenRecord:
        return cls(
            id=d["id"],
            label=d["label"],
            scope=d["scope"],
            hash=d["hash"],
            created_at=d["created_at"],
            last_seen_at=d.get("last_seen_at"),
            expires_at=d.get("expires_at"),
        )

    def is_expired(self, now: _dt.datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        now = now or _dt.datetime.now(_dt.UTC)
        try:
            exp = _dt.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            # Malformed expires_at — treat as expired (fail closed).
            return True
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=_dt.UTC)
        return now >= exp


_DPAPI_SUFFIX = ".dpapi"


def _is_dpapi_path(path: Path) -> bool:
    return path.suffix == _DPAPI_SUFFIX


class TokenStore:
    """File-backed token table with mtime-driven hot reload.

    The path's suffix decides the on-disk format:
      - `tokens.json`        → read as UTF-8 JSON.
      - `tokens.json.dpapi`  → read as raw bytes, decrypted via
        `service._dpapi.unprotect()` (Windows DPAPI under the current
        user). Raises on non-Windows hosts.

    The decision is per-path so Linux test runs that pass a plaintext
    path never touch the DPAPI shim, while Windows production reads the
    encrypted blob without intermediate plaintext on disk.
    """

    def __init__(self, path: Path | None):
        self._path = path
        self._records: dict[str, TokenRecord] = {}  # keyed by hash for O(1) lookup
        self._mtime: float | None = None
        if path is not None and path.exists():
            self.reload()

    @property
    def path(self) -> Path | None:
        return self._path

    def __len__(self) -> int:
        return len(self._records)

    def reload(self) -> None:
        """Force a re-read of the backing file. Idempotent.

        Decryption errors are NOT swallowed — a corrupt or wrong-user
        DPAPI blob must surface (per `_dpapi.unprotect` docstring) so the
        operator does not silently end up with a zero-token table that
        denies every request.
        """
        if self._path is None:
            return
        try:
            raw_bytes = self._path.read_bytes()
            self._mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._records = {}
            self._mtime = None
            return
        if _is_dpapi_path(self._path):
            plaintext = _dpapi.unprotect(raw_bytes)
        else:
            plaintext = raw_bytes
        self.load_dict(json.loads(plaintext.decode("utf-8")))

    def load_dict(self, payload: Mapping[str, Any]) -> None:
        """Replace the in-memory table from a parsed dict. Used by the
        DPAPI loader (which has the decrypted bytes) or by tests."""
        records = {}
        for entry in payload.get("tokens", []):
            rec = TokenRecord.from_dict(entry)
            records[rec.hash] = rec
        self._records = records

    def reload_if_changed(self) -> bool:
        """mtime-driven cheap-check reload. Returns True if reloaded."""
        if self._path is None or not self._path.exists():
            return False
        mtime = self._path.stat().st_mtime
        if self._mtime is None or mtime > self._mtime:
            self.reload()
            return True
        return False

    def lookup(self, presented_secret: str) -> TokenRecord | None:
        """Return the record whose stored hash matches the presented
        bearer secret, else None. Constant-time per record."""
        candidate_hash = hash_secret(presented_secret)
        # Direct dict hit handles the common case; falls back to
        # constant-time scan to avoid leaking presence via timing.
        rec = self._records.get(candidate_hash)
        if rec is not None:
            return rec
        # Scan with compare_digest so a partial-match attempt against a
        # large table doesn't telegraph table size via lookup latency.
        any_match: TokenRecord | None = None
        for stored_hash, candidate in self._records.items():
            if secrets.compare_digest(candidate_hash, stored_hash):
                any_match = candidate
        return any_match


# ---- ASGI middleware -------------------------------------------------

# Mapping shape: `(method, path_prefix) -> required_scope`. The matcher
# is conservative — longest path-prefix match wins. Methods are matched
# exactly (no case fuzziness). Anything unmapped defaults to the most
# restrictive scope.
ScopeRule = tuple[str, str, str]  # (method, path_prefix, required_scope)


# Per-route default scopes.
DEFAULT_SCOPE_RULES: tuple[ScopeRule, ...] = (
    ("GET", "/health", "health"),
    ("GET", "/services/status", "health"),
    ("GET", "/services/last-deploy-tail", "read"),
    ("GET", "/runtime/", "health"),  # /runtime/{slot}/status
    ("GET", "/projects/", "read"),  # /projects, /projects/{p}/files/{path}, /projects/{p}/git/log
    ("GET", "/projects", "read"),
    ("POST", "/projects/", "deploy"),  # /projects/{p}/deploy + /projects/{p}/deploy/preflight
    # MCP transport — single endpoint that multiplexes tools. We require
    # `read` at the transport layer because MCP `initialize` and
    # `tools/list` are read-shaped; per-tool scope refinement happens at
    # the tool dispatch site.
    ("POST", "/mcp", "read"),
    ("GET", "/mcp", "read"),
)


def resolve_required_scope(
    method: str, path: str, rules: tuple[ScopeRule, ...] = DEFAULT_SCOPE_RULES
) -> str:
    """Return the required scope for `(method, path)` per `rules`.

    Longest-prefix match wins. Falls back to `deploy` (most restrictive)
    so an unmapped route fails closed. New routes MUST be added to the
    rules table; the fallback exists to make a missing-rule bug a 403,
    not a privilege escalation.
    """
    best: tuple[int, str] | None = None  # (prefix_len, scope)
    for rule_method, prefix, scope in rules:
        if rule_method != method:
            continue
        if path == prefix or path.startswith(prefix):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), scope)
    if best is None:
        return "deploy"
    return best[1]


# ---- response envelope ----

# Map our internal error codes to RFC 6750 bearer-error strings. OAuth-aware
# clients (e.g. mcp-remote, which Claude Desktop's .mcpb wraps) treat a 401 as an
# OAuth challenge and try to parse the body as an OAuth error — they REQUIRE a
# string `error` field and crash with a cryptic "Invalid OAuth error response"
# without it. Emitting the OAuth shape turns that into a clean auth message.
_OAUTH_ERROR = {
    "auth_required": "invalid_request",       # no/blank credentials
    "auth_invalid": "invalid_token",          # bad or expired token
    "auth_scope_insufficient": "insufficient_scope",
}


def _oauth_error(code: str) -> str:
    return _OAUTH_ERROR.get(code, "invalid_token")


def _error_payload(code: str, message: str, hint: str) -> bytes:
    return json.dumps({
        # RFC 6750 / OAuth 2.0 shape FIRST, so OAuth-aware clients read a real
        # error string instead of choking on a missing `error`.
        "error": _oauth_error(code),
        "error_description": message,
        # ftx-mcp's richer fields, kept for humans + existing clients.
        "code": code,
        "message": message,
        "hint": hint,
        "docs_url": "docs/security.md#bearer-tokens",
    }).encode("utf-8")


def _www_authenticate(code: str, message: str) -> bytes:
    # RFC 6750 §3: a bearer-protected resource returns a WWW-Authenticate: Bearer
    # challenge on 401/403, carrying the error + description. Sanitize the
    # description so it can't break the header (strip quotes / control chars).
    err = _oauth_error(code)
    desc = "".join(c for c in message if c.isprintable()).replace('"', "'")
    return f'Bearer error="{err}", error_description="{desc}"'.encode("latin-1", "replace")


async def _send_error(send: Callable[[Mapping[str, Any]], Awaitable[None]],
                      status: int, code: str, message: str, hint: str) -> None:
    body = _error_payload(code, message, hint)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"www-authenticate", _www_authenticate(code, message)),
        ],
    })
    await send({"type": "http.response.body", "body": body})


_BEARER_PREFIX = "Bearer "


def _extract_bearer(scope: Mapping[str, Any]) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            text = value.decode("latin-1", errors="replace")
            if text.startswith(_BEARER_PREFIX):
                return text[len(_BEARER_PREFIX):].strip()
            return None
    return None


class AuthMiddleware:
    """ASGI middleware enforcing bearer-token auth.

    When `auth_required=False` (the DEFAULT — `FTX_AUTH_REQUIRED` defaults
    to "false" in Config; loopback bind only, see
    `main.check_lan_bind_safety`), missing-header requests pass through.
    When `auth_required=True` (the `FTX_AUTH_REQUIRED=true` opt-in), every
    request must present a valid token covering the route's scope.

    The middleware is mountable on both `FastAPI` and on the Starlette
    app returned by `FastMCP.streamable_http_app()`; scope rules
    distinguish the surfaces by path.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        store: TokenStore,
        *,
        auth_required: bool,
        rules: tuple[ScopeRule, ...] = DEFAULT_SCOPE_RULES,
        on_token_seen: Callable[[str], None] | None = None,
    ):
        self.app = app
        self.store = store
        self.auth_required = auth_required
        self.rules = rules
        self._on_token_seen = on_token_seen

    async def __call__(self, scope: Mapping[str, Any],
                       receive: Callable[..., Awaitable[Any]],
                       send: Callable[..., Awaitable[None]]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # mtime-driven hot reload — cheap stat() per request, idempotent.
        self.store.reload_if_changed()

        method = scope.get("method", "")
        path = scope.get("path", "")
        required = resolve_required_scope(method, path, self.rules)

        presented = _extract_bearer(scope)
        if presented is None:
            if not self.auth_required:
                await self.app(scope, receive, send)
                return
            await _send_error(
                send, 401, "auth_required",
                "this surface requires Authorization: Bearer <token>",
                "issue a token via bootstrap/issue-token.ps1, then set "
                "Authorization on subsequent requests",
            )
            return

        record = self.store.lookup(presented)
        if record is None:
            await _send_error(
                send, 401, "auth_invalid",
                "presented token does not match any issued credential",
                "re-issue via bootstrap/issue-token.ps1 or check the "
                "client's stored secret",
            )
            return

        if record.is_expired():
            await _send_error(
                send, 401, "auth_invalid",
                f"token {record.id} has expired",
                "re-issue via bootstrap/issue-token.ps1",
            )
            return

        if not scope_satisfies(record.scope, required):
            await _send_error(
                send, 403, "auth_scope_insufficient",
                f"token has scope {record.scope!r} but {required!r} is required",
                "re-issue with a higher scope (deploy ⊇ read ⊇ health) "
                "via bootstrap/issue-token.ps1",
            )
            return

        if self._on_token_seen is not None:
            try:
                self._on_token_seen(record.id)
            except Exception:
                # last_seen_at is best-effort — never
                # block the request on bookkeeping failures.
                pass

        # Pass token id through scope for downstream logging. The cast
        # to a fresh dict avoids mutating the caller's mapping.
        forwarded = dict(scope)
        forwarded["ftxm.token_id"] = record.id
        forwarded["ftxm.token_scope"] = record.scope
        await self.app(forwarded, receive, send)


# ---- helpers for the issue-token PS1 round-trip ---------------------

def serialize_record(rec: TokenRecord) -> dict[str, Any]:
    """Serialize a TokenRecord to the JSON shape persisted in
    tokens.json. Inverse of TokenRecord.from_dict."""
    return {
        "id": rec.id,
        "label": rec.label,
        "scope": rec.scope,
        "hash": rec.hash,
        "created_at": rec.created_at,
        "last_seen_at": rec.last_seen_at,
        "expires_at": rec.expires_at,
    }


def now_iso() -> str:
    """Return current UTC time in the design's ISO8601-Z format."""
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "AuthMiddleware",
    "DEFAULT_SCOPE_RULES",
    "SCOPES",
    "TokenRecord",
    "TokenStore",
    "generate_token",
    "hash_secret",
    "now_iso",
    "resolve_required_scope",
    "scope_satisfies",
    "serialize_record",
]
