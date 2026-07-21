"""Tests for service.main — LAN-bind refusal matrix + middleware wiring.

Per docs/phase2-design.md §2.1, the refusal matrix has six branches.
Each one is exercised here without spinning up uvicorn.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from service import auth, core, main


def _cfg(
    base: core.Config, *, bind_host: str, auth_required: bool, tokens_path: Path | None
) -> core.Config:
    return replace(
        base,
        bind_host=bind_host,
        auth_required=auth_required,
        tokens_path=tokens_path,
    )


def _store_with_n_tokens(n: int) -> auth.TokenStore:
    store = auth.TokenStore(path=None)
    payload = {"tokens": []}
    for i in range(n):
        token_id, secret = auth.generate_token()
        payload["tokens"].append({
            "id": token_id,
            "label": f"test-{i}",
            "scope": "read",
            "hash": auth.hash_secret(secret),
            "created_at": "2026-05-06T00:00:00Z",
            "last_seen_at": None,
            "expires_at": None,
        })
    store.load_dict(payload)
    return store


# ---- refusal matrix branches ---------------------------------------

class TestRefusalMatrix:
    def test_loopback_no_auth_starts(self, cfg: core.Config) -> None:
        c = _cfg(cfg, bind_host="127.0.0.1", auth_required=False, tokens_path=None)
        store = _store_with_n_tokens(0)
        exit_code, fails, warns = main.check_lan_bind_safety(c, store)
        assert exit_code == 0
        assert fails == []
        assert warns == []

    def test_loopback_auth_with_tokens_starts(self, cfg: core.Config) -> None:
        c = _cfg(cfg, bind_host="127.0.0.1", auth_required=True, tokens_path=None)
        store = _store_with_n_tokens(1)
        exit_code, fails, warns = main.check_lan_bind_safety(c, store)
        assert exit_code == 0
        assert fails == []
        assert warns == []

    def test_loopback_auth_no_tokens_starts_with_warn(self, cfg: core.Config) -> None:
        c = _cfg(cfg, bind_host="127.0.0.1", auth_required=True, tokens_path=None)
        store = _store_with_n_tokens(0)
        exit_code, fails, warns = main.check_lan_bind_safety(c, store)
        assert exit_code == 0
        assert fails == []
        assert any("no tokens have been issued" in w for w in warns)

    def test_lan_bind_no_auth_refused_exit_3(self, cfg: core.Config) -> None:
        c = _cfg(cfg, bind_host="0.0.0.0", auth_required=False, tokens_path=None)
        store = _store_with_n_tokens(0)
        exit_code, fails, warns = main.check_lan_bind_safety(c, store)
        assert exit_code == 3
        assert any("FTX_AUTH_REQUIRED=false" in f for f in fails)
        assert any("LAN bind" in f for f in fails)
        assert any("loopback-no-auth opt-out" in f for f in fails)

    def test_lan_bind_auth_no_tokens_refused_exit_3(self, cfg: core.Config) -> None:
        c = _cfg(cfg, bind_host="0.0.0.0", auth_required=True, tokens_path=None)
        store = _store_with_n_tokens(0)
        exit_code, fails, warns = main.check_lan_bind_safety(c, store)
        assert exit_code == 3
        assert any("no tokens issued" in f for f in fails)
        assert any("issue-token.ps1" in f for f in fails)

    def test_lan_bind_auth_with_tokens_starts_with_warn(self, cfg: core.Config) -> None:
        c = _cfg(cfg, bind_host="192.0.2.1", auth_required=True, tokens_path=None)
        store = _store_with_n_tokens(2)
        exit_code, fails, warns = main.check_lan_bind_safety(c, store)
        assert exit_code == 0
        assert fails == []
        assert any("LAN bind" in w and "2 token" in w for w in warns)

    def test_non_loopback_localhost_not_treated_as_loopback(self, cfg: core.Config) -> None:
        # The matrix gates on the literal string "127.0.0.1". A box that
        # binds to "localhost" or "::1" or "0.0.0.0" is treated as
        # non-loopback (because the os.environ value is what gets compared).
        # This is the conservative shape — fail closed on anything but the
        # canonical loopback string.
        c = _cfg(cfg, bind_host="localhost", auth_required=False, tokens_path=None)
        store = _store_with_n_tokens(0)
        exit_code, _fails, _warns = main.check_lan_bind_safety(c, store)
        assert exit_code == 3


# ---- Config from_env wiring ----------------------------------------

class TestConfigEnvWiring:
    def test_auth_required_default_false(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # v0.2.1 flipped False->True; v1.0 flips it back to False for the
        # loopback-only common case (any local process runs as you). The
        # non-loopback bind still refuses without auth (main.py LAN guard), so
        # the default-off is safe. Absent env => auth OFF.
        for var in ("FTX_AUTH_REQUIRED", "OPTIX_AUTH_REQUIRED", "OPTIX_TOKENS_PATH"):
            monkeypatch.delenv(var, raising=False)
        # other required envs
        monkeypatch.setenv("OPTIX_PROJECTS_ROOT", "/tmp")
        monkeypatch.setenv("FTOPTIX_STUDIO_EXE", "/tmp/Studio.exe")
        monkeypatch.setenv("OPTIX_STATE_DIR", "/tmp/state")
        c = core.Config.from_env()
        assert c.auth_required is False

    def test_auth_required_truthy_values(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("OPTIX_PROJECTS_ROOT", "/tmp")
        monkeypatch.setenv("FTOPTIX_STUDIO_EXE", "/tmp/Studio.exe")
        monkeypatch.setenv("OPTIX_STATE_DIR", "/tmp/state")
        for value in ("true", "1", "yes", "ON", "True", "  TRUE  "):
            monkeypatch.setenv("FTX_AUTH_REQUIRED", value)
            c = core.Config.from_env()
            assert c.auth_required is True, f"{value!r} should be truthy"

    def test_auth_required_falsy_values(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("OPTIX_PROJECTS_ROOT", "/tmp")
        monkeypatch.setenv("FTOPTIX_STUDIO_EXE", "/tmp/Studio.exe")
        monkeypatch.setenv("OPTIX_STATE_DIR", "/tmp/state")
        for value in ("false", "0", "no", "off", "", "   "):
            monkeypatch.setenv("FTX_AUTH_REQUIRED", value)
            c = core.Config.from_env()
            assert c.auth_required is False, f"{value!r} should be falsy"

    def test_old_optix_env_var_name_ignored(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # 2026-05-11 rename: OPTIX_AUTH_REQUIRED is dead; only FTX_AUTH_REQUIRED
        # is read. Set the stale var to `true` (opposite of the v1.0 default) and
        # confirm it's ignored — auth falls back to the default (false), not on.
        monkeypatch.delenv("FTX_AUTH_REQUIRED", raising=False)
        monkeypatch.setenv("OPTIX_AUTH_REQUIRED", "true")
        monkeypatch.setenv("OPTIX_PROJECTS_ROOT", "/tmp")
        monkeypatch.setenv("FTOPTIX_STUDIO_EXE", "/tmp/Studio.exe")
        monkeypatch.setenv("OPTIX_STATE_DIR", "/tmp/state")
        c = core.Config.from_env()
        assert c.auth_required is False

    def test_tokens_path_default_under_state_dir(self, monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("OPTIX_TOKENS_PATH", raising=False)
        monkeypatch.setenv("OPTIX_PROJECTS_ROOT", "/tmp")
        monkeypatch.setenv("FTOPTIX_STUDIO_EXE", "/tmp/Studio.exe")
        monkeypatch.setenv("OPTIX_STATE_DIR", str(tmp_path))
        c = core.Config.from_env()
        assert c.tokens_path == tmp_path / "secrets" / "tokens.json.dpapi"

    def test_tokens_path_env_override(self, monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        custom = tmp_path / "elsewhere" / "tokens.json"
        monkeypatch.setenv("OPTIX_TOKENS_PATH", str(custom))
        monkeypatch.setenv("OPTIX_PROJECTS_ROOT", "/tmp")
        monkeypatch.setenv("FTOPTIX_STUDIO_EXE", "/tmp/Studio.exe")
        monkeypatch.setenv("OPTIX_STATE_DIR", str(tmp_path))
        c = core.Config.from_env()
        assert c.tokens_path == custom


# ---- middleware-wrapping helper ------------------------------------

class TestWrapWithAuth:
    def test_returns_auth_middleware_instance(self) -> None:
        async def app(_scope, _receive, _send):  # type: ignore[no-untyped-def]
            return None

        store = auth.TokenStore(path=None)
        wrapped = main._wrap_with_auth(app, store, auth_required=True)
        assert isinstance(wrapped, auth.AuthMiddleware)
        assert wrapped.auth_required is True
        assert wrapped.store is store
        assert wrapped.app is app

    def test_propagates_auth_required_false(self) -> None:
        async def app(_scope, _receive, _send):  # type: ignore[no-untyped-def]
            return None

        store = auth.TokenStore(path=None)
        wrapped = main._wrap_with_auth(app, store, auth_required=False)
        assert wrapped.auth_required is False


# ---- token store builder -------------------------------------------

class TestBuildTokenStore:
    def test_missing_file_yields_empty_store(self, cfg: core.Config, tmp_path: Path) -> None:
        c = replace(cfg, tokens_path=tmp_path / "absent.json")
        store = main.build_token_store(c)
        assert len(store) == 0

    def test_loads_existing_file(self, cfg: core.Config, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        token_id, secret = auth.generate_token()
        import json
        path.write_text(json.dumps({
            "tokens": [{
                "id": token_id,
                "label": "first",
                "scope": "read",
                "hash": auth.hash_secret(secret),
                "created_at": "2026-05-06T00:00:00Z",
                "last_seen_at": None,
                "expires_at": None,
            }]
        }))

        c = replace(cfg, tokens_path=path)
        store = main.build_token_store(c)
        assert len(store) == 1
        assert store.lookup(secret) is not None


# ---- startup state-dir self-creation --------------------------------

class TestEnsureStateDirs:
    """v1.0.1 (field report finding 1): the service self-creates runtime_dir
    at startup, exactly as it already did state_dir. An installer run from an
    MSIX-packaged shell (Store-build Claude Desktop / Cowork quick-install)
    gets its %LOCALAPPDATA% mkdirs virtualized into the app's package
    overlay — the scheduled task then launches the service against the real
    filesystem where the dirs never existed (/health runtime_dir_exists:
    false while the install shell swears they are present).
    """

    def test_creates_both_state_and_runtime_dirs(
        self, cfg: core.Config, tmp_path: Path
    ) -> None:
        c = replace(
            cfg,
            state_dir=tmp_path / "fresh-state",
            runtime_dir=tmp_path / "fresh-state" / "runtime",
        )
        main._ensure_state_dirs(c)
        assert c.state_dir.is_dir()
        assert c.runtime_dir.is_dir()

    def test_runtime_dir_none_is_tolerated(
        self, cfg: core.Config, tmp_path: Path
    ) -> None:
        c = replace(cfg, state_dir=tmp_path / "s", runtime_dir=None)
        main._ensure_state_dirs(c)
        assert c.state_dir.is_dir()

    def test_uncreatable_runtime_dir_does_not_crash_startup(
        self, cfg: core.Config, tmp_path: Path
    ) -> None:
        # runtime_dir nested under a FILE -> mkdir raises OSError; startup
        # must swallow it so the condition surfaces as a red /health flag
        # (split-volume OPTIX_RUNTIME_DIR on an offline drive), not a crash.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        c = replace(
            cfg,
            state_dir=tmp_path / "s2",
            runtime_dir=blocker / "runtime",
        )
        main._ensure_state_dirs(c)
        assert c.state_dir.is_dir()
        assert not c.runtime_dir.exists()
