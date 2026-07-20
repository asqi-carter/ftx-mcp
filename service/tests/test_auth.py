"""Tests for service.auth — bearer-token model + ASGI middleware.

Linux-runner-friendly: no DPAPI, no Windows. Token JSON is staged to a
plain file; the production loader will decrypt `tokens.json.dpapi` to
the same in-memory shape before calling `TokenStore.load_dict`.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from service import auth

# ---- scope helpers ---------------------------------------------------

class TestScopeSatisfies:
    def test_health_satisfies_health(self) -> None:
        assert auth.scope_satisfies("health", "health") is True

    def test_read_satisfies_health(self) -> None:
        assert auth.scope_satisfies("read", "health") is True

    def test_deploy_satisfies_read(self) -> None:
        assert auth.scope_satisfies("deploy", "read") is True

    def test_deploy_satisfies_deploy(self) -> None:
        assert auth.scope_satisfies("deploy", "deploy") is True

    def test_health_does_not_satisfy_read(self) -> None:
        assert auth.scope_satisfies("health", "read") is False

    def test_read_does_not_satisfy_deploy(self) -> None:
        assert auth.scope_satisfies("read", "deploy") is False

    def test_unknown_token_scope_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown token scope"):
            auth.scope_satisfies("admin", "read")

    def test_unknown_required_scope_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown required scope"):
            auth.scope_satisfies("read", "admin")


# ---- token gen + hash -----------------------------------------------

class TestGenerateToken:
    def test_secret_has_ftxm_prefix(self) -> None:
        _id, secret = auth.generate_token()
        assert secret.startswith("ftxm_")

    def test_secret_is_url_safe_base64(self) -> None:
        _id, secret = auth.generate_token()
        body = secret[len("ftxm_") :]
        # urlsafe base64 alphabet is A-Z a-z 0-9 - _
        assert all(c.isalnum() or c in "-_" for c in body)

    def test_id_is_32_hex_chars(self) -> None:
        token_id, _ = auth.generate_token()
        assert len(token_id) == 32
        assert all(c in "0123456789abcdef" for c in token_id)

    def test_two_tokens_differ(self) -> None:
        a_id, a_secret = auth.generate_token()
        b_id, b_secret = auth.generate_token()
        assert a_id != b_id
        assert a_secret != b_secret


class TestHashSecret:
    def test_hash_format(self) -> None:
        h = auth.hash_secret("ftxm_abc")
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64  # sha256 hex digest length

    def test_hash_is_deterministic(self) -> None:
        a = auth.hash_secret("ftxm_xxx")
        b = auth.hash_secret("ftxm_xxx")
        assert a == b

    def test_different_secrets_hash_differently(self) -> None:
        a = auth.hash_secret("ftxm_aaa")
        b = auth.hash_secret("ftxm_bbb")
        assert a != b


# ---- TokenRecord -----------------------------------------------------

def make_record(
    *, scope: str = "read", expires_at: str | None = None, label: str = "test"
) -> auth.TokenRecord:
    _id, secret = auth.generate_token()
    return auth.TokenRecord(
        id=_id,
        label=label,
        scope=scope,
        hash=auth.hash_secret(secret),
        created_at="2026-05-06T00:00:00Z",
        expires_at=expires_at,
    ), secret  # type: ignore[return-value]


class TestTokenRecord:
    def test_no_expires_means_not_expired(self) -> None:
        rec, _ = make_record()
        assert rec.is_expired() is False

    def test_future_expires_not_expired(self) -> None:
        future = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rec, _ = make_record(expires_at=future)
        assert rec.is_expired() is False

    def test_past_expires_is_expired(self) -> None:
        past = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rec, _ = make_record(expires_at=past)
        assert rec.is_expired() is True

    def test_malformed_expires_fails_closed(self) -> None:
        rec, _ = make_record(expires_at="not-a-date")
        assert rec.is_expired() is True

    def test_from_dict_roundtrip(self) -> None:
        rec, _ = make_record(label="rt")
        d = auth.serialize_record(rec)
        recovered = auth.TokenRecord.from_dict(d)
        assert recovered == rec


# ---- TokenStore ------------------------------------------------------

def write_tokens_file(path: Path, records: list[auth.TokenRecord]) -> None:
    path.write_text(json.dumps({"tokens": [auth.serialize_record(r) for r in records]}))


class TestTokenStore:
    def test_load_dict_populates_table(self) -> None:
        rec, secret = make_record()
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})
        assert len(store) == 1
        assert store.lookup(secret) == rec

    def test_lookup_unknown_secret_returns_none(self) -> None:
        rec, _secret = make_record()
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})
        assert store.lookup("ftxm_does_not_match") is None

    def test_reload_picks_up_new_tokens(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        rec_a, secret_a = make_record(label="a")
        write_tokens_file(path, [rec_a])

        store = auth.TokenStore(path=path)
        assert store.lookup(secret_a) == rec_a

        rec_b, secret_b = make_record(label="b")
        # nudge mtime forward — same-second writes can hash to identical
        # mtimes on some filesystems.
        import os
        future = path.stat().st_mtime + 1
        write_tokens_file(path, [rec_a, rec_b])
        os.utime(path, (future, future))

        assert store.reload_if_changed() is True
        assert store.lookup(secret_a) == rec_a
        assert store.lookup(secret_b) == rec_b

    def test_reload_if_unchanged_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        rec, _ = make_record()
        write_tokens_file(path, [rec])
        store = auth.TokenStore(path=path)
        assert store.reload_if_changed() is False

    def test_missing_file_yields_empty_store(self, tmp_path: Path) -> None:
        path = tmp_path / "does-not-exist.json"
        store = auth.TokenStore(path=path)
        assert len(store) == 0
        assert store.lookup("ftxm_anything") is None


# ---- TokenStore .dpapi-suffix handling ------------------------------

class TestTokenStoreDpapi:
    """The `.dpapi` suffix routes reads through `_dpapi.unprotect`.

    On Linux the underlying ctypes call is unavailable, so the store
    must surface `UnsupportedPlatformError` rather than silently land
    on a zero-token table (which would deny every authenticated request
    in production).
    """

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="UnsupportedPlatformError is the Linux/macOS guardrail")
    def test_dpapi_suffix_on_linux_raises_unsupported(self, tmp_path: Path) -> None:
        from service import _dpapi
        path = tmp_path / "tokens.json.dpapi"
        path.write_bytes(b"\x00\x01\x02any-ciphertext-blob")
        with pytest.raises(_dpapi.UnsupportedPlatformError, match="Windows"):
            auth.TokenStore(path=path)

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="real DPAPI is Windows-only; Linux uses the monkeypatched test above")
    def test_dpapi_suffix_on_windows_real_round_trip(self, tmp_path: Path) -> None:
        """End-to-end DPAPI: encrypt with `_dpapi.protect`, write to disk,
        read back via TokenStore. Catches integration regressions in the
        ctypes plumbing that the Linux monkeypatch tests cannot see."""
        from service import _dpapi
        rec, secret = make_record(label="real-dpapi")
        plaintext = json.dumps({"tokens": [auth.serialize_record(rec)]}).encode("utf-8")
        ciphertext = _dpapi.protect(plaintext)
        path = tmp_path / "tokens.json.dpapi"
        path.write_bytes(ciphertext)

        store = auth.TokenStore(path=path)
        assert store.lookup(secret) == rec

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="CryptUnprotectData failure semantics are Windows-only")
    def test_dpapi_suffix_on_windows_corrupt_blob_raises_os_error(self, tmp_path: Path) -> None:
        """Corrupt or wrong-user blob must surface as OSError so the
        operator knows decryption failed rather than silently landing on
        a zero-token table that denies every request."""
        path = tmp_path / "tokens.json.dpapi"
        path.write_bytes(b"\x00\x01\x02not-a-real-dpapi-blob")
        with pytest.raises(OSError, match="CryptUnprotectData"):
            auth.TokenStore(path=path)

    def test_dpapi_suffix_decrypts_via_shim(self, monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """Monkeypatch unprotect so we can exercise the .dpapi → JSON
        path on Linux without needing CryptUnprotectData."""
        rec, secret = make_record(label="dpapi")
        plaintext = json.dumps({"tokens": [auth.serialize_record(rec)]}).encode("utf-8")
        ciphertext = b"FAKE-CIPHERTEXT::" + plaintext
        path = tmp_path / "tokens.json.dpapi"
        path.write_bytes(ciphertext)

        from service import _dpapi
        seen: list[bytes] = []

        def fake_unprotect(blob: bytes, *, scope: str = "CurrentUser") -> bytes:
            seen.append(blob)
            assert blob.startswith(b"FAKE-CIPHERTEXT::")
            return blob[len(b"FAKE-CIPHERTEXT::"):]

        monkeypatch.setattr(_dpapi, "unprotect", fake_unprotect)
        store = auth.TokenStore(path=path)
        assert seen == [ciphertext]
        assert store.lookup(secret) == rec

    def test_dpapi_hot_reload_re_decrypts(self, monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """mtime-driven reload must run through `unprotect` on each
        change so revoked tokens disappear without a service restart."""
        rec_a, secret_a = make_record(label="a")
        rec_b, secret_b = make_record(label="b")

        from service import _dpapi

        def fake_unprotect(blob: bytes, *, scope: str = "CurrentUser") -> bytes:
            return blob[len(b"FAKE::"):]

        monkeypatch.setattr(_dpapi, "unprotect", fake_unprotect)

        path = tmp_path / "tokens.json.dpapi"

        def write(records: list[auth.TokenRecord]) -> None:
            payload = json.dumps({"tokens": [auth.serialize_record(r) for r in records]})
            path.write_bytes(b"FAKE::" + payload.encode("utf-8"))

        write([rec_a])
        store = auth.TokenStore(path=path)
        assert store.lookup(secret_a) == rec_a
        assert store.lookup(secret_b) is None

        import os
        future = path.stat().st_mtime + 1
        write([rec_b])
        os.utime(path, (future, future))

        assert store.reload_if_changed() is True
        assert store.lookup(secret_a) is None
        assert store.lookup(secret_b) == rec_b

    def test_plaintext_path_skips_dpapi_shim(self, monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """`.json` path must NOT touch `_dpapi.unprotect`; the suffix is
        the only signal that decides format."""
        from service import _dpapi

        def boom(*args: Any, **kwargs: Any) -> bytes:
            raise AssertionError("_dpapi.unprotect must not be called for .json paths")

        monkeypatch.setattr(_dpapi, "unprotect", boom)

        rec, secret = make_record()
        path = tmp_path / "tokens.json"
        write_tokens_file(path, [rec])
        store = auth.TokenStore(path=path)
        assert store.lookup(secret) == rec


# ---- resolve_required_scope -----------------------------------------

class TestResolveRequiredScope:
    def test_health_endpoint(self) -> None:
        assert auth.resolve_required_scope("GET", "/health") == "health"

    def test_services_status(self) -> None:
        assert auth.resolve_required_scope("GET", "/services/status") == "health"

    def test_runtime_status(self) -> None:
        assert auth.resolve_required_scope("GET", "/runtime/test/status") == "health"

    def test_projects_list(self) -> None:
        assert auth.resolve_required_scope("GET", "/projects") == "read"

    def test_project_files(self) -> None:
        assert auth.resolve_required_scope("GET", "/projects/foo/files/screen.yaml") == "read"

    def test_project_git_log(self) -> None:
        assert auth.resolve_required_scope("GET", "/projects/foo/git/log") == "read"

    def test_last_deploy_tail(self) -> None:
        assert auth.resolve_required_scope("GET", "/services/last-deploy-tail") == "read"

    def test_deploy_endpoint(self) -> None:
        assert auth.resolve_required_scope("POST", "/projects/foo/deploy") == "deploy"

    def test_deploy_preflight(self) -> None:
        assert auth.resolve_required_scope("POST", "/projects/foo/deploy/preflight") == "deploy"

    def test_mcp_post(self) -> None:
        assert auth.resolve_required_scope("POST", "/mcp") == "read"

    def test_unmapped_route_falls_back_to_deploy(self) -> None:
        # Unmapped path defaults to most-restrictive — fail closed.
        assert auth.resolve_required_scope("DELETE", "/admin/wipe") == "deploy"


# ---- AuthMiddleware (ASGI) ------------------------------------------

class CapturingApp:
    """Minimal ASGI app that records the scope it was invoked with and
    returns a 200 with empty body."""

    def __init__(self) -> None:
        self.last_scope: dict[str, Any] | None = None
        self.call_count = 0

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        self.last_scope = dict(scope)
        self.call_count += 1
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": b"{}"})


class ResponseCapture:
    """Collects the ASGI messages the middleware sends downstream."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, message) -> None:  # type: ignore[no-untyped-def]
        self.messages.append(dict(message))

    @property
    def status(self) -> int:
        for m in self.messages:
            if m.get("type") == "http.response.start":
                return int(m["status"])
        raise AssertionError("no response.start emitted")

    @property
    def body(self) -> bytes:
        out = b""
        for m in self.messages:
            if m.get("type") == "http.response.body":
                out += m.get("body", b"")
        return out

    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


def make_scope(method: str, path: str, *, auth_header: str | None = None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = []
    if auth_header is not None:
        headers.append((b"authorization", auth_header.encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
    }


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_no_header_passes_when_auth_not_required(self) -> None:
        store = auth.TokenStore(path=None)
        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=False)
        capture = ResponseCapture()

        await mw(make_scope("GET", "/health"), _noop_receive, capture.send)

        assert capture.status == 200
        assert inner.call_count == 1

    @pytest.mark.asyncio
    async def test_no_header_rejected_when_auth_required(self) -> None:
        store = auth.TokenStore(path=None)
        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=True)
        capture = ResponseCapture()

        await mw(make_scope("GET", "/health"), _noop_receive, capture.send)

        assert capture.status == 401
        assert capture.json()["code"] == "auth_required"
        assert inner.call_count == 0

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self) -> None:
        rec, _real_secret = make_record(scope="read")
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})

        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=True)
        capture = ResponseCapture()

        await mw(
            make_scope("GET", "/health", auth_header="Bearer ftxm_wrong"),
            _noop_receive, capture.send,
        )

        assert capture.status == 401
        assert capture.json()["code"] == "auth_invalid"
        assert inner.call_count == 0

    @pytest.mark.asyncio
    async def test_401_is_oauth_shaped_for_mcp_remote(self) -> None:
        # RFC 6750 shape so OAuth-aware clients (mcp-remote / the .mcpb) parse a
        # clean auth error instead of crashing on a missing `error` field.
        rec, _ = make_record(scope="read")
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})
        mw = auth.AuthMiddleware(CapturingApp(), store, auth_required=True)
        capture = ResponseCapture()
        await mw(
            make_scope("GET", "/health", auth_header="Bearer ftxm_wrong"),
            _noop_receive, capture.send,
        )
        body = capture.json()
        assert body["error"] == "invalid_token"          # OAuth error string
        assert body["error_description"]                  # non-empty
        assert body["code"] == "auth_invalid"             # our field retained
        # WWW-Authenticate: Bearer challenge present
        start = next(m for m in capture.messages if m.get("type") == "http.response.start")
        hdrs = {k.lower(): v for k, v in start["headers"]}
        assert hdrs[b"www-authenticate"].startswith(b'Bearer error="invalid_token"')

    @pytest.mark.asyncio
    async def test_401_no_header_is_invalid_request(self) -> None:
        store = auth.TokenStore(path=None)
        mw = auth.AuthMiddleware(CapturingApp(), store, auth_required=True)
        capture = ResponseCapture()
        await mw(make_scope("GET", "/health"), _noop_receive, capture.send)
        assert capture.json()["error"] == "invalid_request"

    @pytest.mark.asyncio
    async def test_valid_read_token_passes_health(self) -> None:
        rec, secret = make_record(scope="read")
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})

        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=True)
        capture = ResponseCapture()

        await mw(
            make_scope("GET", "/health", auth_header=f"Bearer {secret}"),
            _noop_receive, capture.send,
        )

        assert capture.status == 200
        assert inner.call_count == 1
        # Middleware annotates downstream scope with token id.
        assert inner.last_scope is not None
        assert inner.last_scope.get("ftxm.token_id") == rec.id
        assert inner.last_scope.get("ftxm.token_scope") == "read"

    @pytest.mark.asyncio
    async def test_health_token_blocked_from_deploy_endpoint(self) -> None:
        rec, secret = make_record(scope="health")
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})

        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=True)
        capture = ResponseCapture()

        await mw(
            make_scope("POST", "/projects/foo/deploy", auth_header=f"Bearer {secret}"),
            _noop_receive, capture.send,
        )

        assert capture.status == 403
        assert capture.json()["code"] == "auth_scope_insufficient"
        assert inner.call_count == 0

    @pytest.mark.asyncio
    async def test_deploy_token_passes_deploy_endpoint(self) -> None:
        rec, secret = make_record(scope="deploy")
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})

        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=True)
        capture = ResponseCapture()

        await mw(
            make_scope("POST", "/projects/foo/deploy", auth_header=f"Bearer {secret}"),
            _noop_receive, capture.send,
        )

        assert capture.status == 200
        assert inner.call_count == 1

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self) -> None:
        past = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rec, secret = make_record(scope="deploy", expires_at=past)
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})

        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=True)
        capture = ResponseCapture()

        await mw(
            make_scope("GET", "/health", auth_header=f"Bearer {secret}"),
            _noop_receive, capture.send,
        )

        assert capture.status == 401
        assert capture.json()["code"] == "auth_invalid"
        assert "expired" in capture.json()["message"]

    @pytest.mark.asyncio
    async def test_non_bearer_auth_header_rejected(self) -> None:
        rec, _ = make_record(scope="read")
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})

        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=True)
        capture = ResponseCapture()

        await mw(
            make_scope("GET", "/health", auth_header="Basic c29tZXVzZXI6cGFzcw=="),
            _noop_receive, capture.send,
        )

        # Non-Bearer header is treated as no-header → auth_required.
        assert capture.status == 401
        assert capture.json()["code"] == "auth_required"
        assert inner.call_count == 0

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self) -> None:
        store = auth.TokenStore(path=None)
        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=True)
        capture = ResponseCapture()

        # ASGI lifespan messages must not get auth-checked.
        lifespan_scope = {"type": "lifespan"}
        await mw(lifespan_scope, _noop_receive, capture.send)
        assert inner.call_count == 1

    @pytest.mark.asyncio
    async def test_on_token_seen_callback_fires_on_success(self) -> None:
        rec, secret = make_record(scope="read")
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})

        seen: list[str] = []
        inner = CapturingApp()
        mw = auth.AuthMiddleware(
            inner, store, auth_required=True,
            on_token_seen=lambda token_id: seen.append(token_id),
        )
        capture = ResponseCapture()

        await mw(
            make_scope("GET", "/health", auth_header=f"Bearer {secret}"),
            _noop_receive, capture.send,
        )

        assert seen == [rec.id]

    @pytest.mark.asyncio
    async def test_on_token_seen_callback_failure_does_not_block(self) -> None:
        rec, secret = make_record(scope="read")
        store = auth.TokenStore(path=None)
        store.load_dict({"tokens": [auth.serialize_record(rec)]})

        def boom(_: str) -> None:
            raise RuntimeError("audit log unreachable")

        inner = CapturingApp()
        mw = auth.AuthMiddleware(inner, store, auth_required=True, on_token_seen=boom)
        capture = ResponseCapture()

        await mw(
            make_scope("GET", "/health", auth_header=f"Bearer {secret}"),
            _noop_receive, capture.send,
        )

        assert capture.status == 200
        assert inner.call_count == 1
