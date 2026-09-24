"""Offline pytest tests for the WebPresentationEngine port resolver,
self-heal path, doctor row coverage, and regression guard.

Covered by this file:
  (1) Resolver priority order — explicit > project-via-bridge > project-via-UI.yaml
      > env-override > default; the no_engine case; cache invalidation.
  (2) Self-heal path (_maybe_follow_port) — dead expected port + live emulator
      produces a resolver re-run; result carries expected/actual/project.
  (3) Doctor row shape — web_port squatter pid, bind-fail signature from fixture.
  (4) Regression — cfg.runtime_test_port env-override path still honoured.

All tests are offline: no live Studio bridge, no network, no real processes.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from service import core


# ---------------------------------------------------------------------------
# Shared autouse isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cache():
    """Wipe the web-port TTL cache before and after each test."""
    core.reset_web_port_cache()
    yield
    core.reset_web_port_cache()


@pytest.fixture(autouse=True)
def _no_bridge_cfg_for(monkeypatch: pytest.MonkeyPatch):
    """By default, make _bridge_cfg_for return None (no armed bridge).

    Individual tests that need the bridge path override this via a secondary
    monkeypatch call inside the test body.
    """
    monkeypatch.setattr(core, "_bridge_cfg_for", lambda cfg, project: None)


@pytest.fixture(autouse=True)
def _no_psutil(monkeypatch: pytest.MonkeyPatch):
    """Keep the emulator PID check deterministic: no real process scan."""
    monkeypatch.setattr(core, "_emulator_pids", lambda: [])


@pytest.fixture(autouse=True)
def _no_tcp_probe(monkeypatch: pytest.MonkeyPatch):
    """TCP probes are stubbed dead by default; tests override as needed."""
    monkeypatch.setattr(core, "_tcp_probe", lambda host, port, **kw: False)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Remove OPTIX_RUNTIME_TEST_PORT from the environment; tests that need it
    set it explicitly."""
    monkeypatch.delenv("OPTIX_RUNTIME_TEST_PORT", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project_with_engine(projects_root: Path, name: str = "MyApp",
                               port: int = 8088) -> Path:
    """Project directory with Nodes/UI/UI.yaml containing a WebPresentationEngine."""
    proj = projects_root / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{name}.optix").write_text("fake-optix-marker")
    ui_dir = proj / "Nodes" / "UI"
    ui_dir.mkdir(parents=True)
    (ui_dir / "UI.yaml").write_text(
        f"- Name: WebPresentationEngine\n"
        f"  Type: FTOptix.WebPresentationEngine.WebPresentationEngine\n"
        f"  Port: {port}\n",
        encoding="utf-8",
    )
    return proj


def _make_project_no_engine(projects_root: Path, name: str = "NoEngine") -> Path:
    """Project directory with UI.yaml but NO WebPresentationEngine node."""
    proj = projects_root / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{name}.optix").write_text("fake-optix-marker")
    ui_dir = proj / "Nodes" / "UI"
    ui_dir.mkdir(parents=True)
    (ui_dir / "UI.yaml").write_text(
        "- Name: SomeOtherWidget\n"
        "  Type: FTOptix.UI.Button\n",
        encoding="utf-8",
    )
    return proj


def _make_bridge_node(port: int) -> dict:
    """Minimal bridge describe_node response with a Port property."""
    return {
        "path": "UI/WebPresentationEngine",
        "browse_name": "WebPresentationEngine",
        "node_class": "Object",
        "dotnet_type": "FTOptix.WebPresentationEngine.WebPresentationEngine",
        "source": "bridge",
        "children": [],
        "properties": [
            {
                "name": "Port",
                "value": f"<untrusted source=\"bridge\">{port}</untrusted>",
            }
        ],
        "truncated": False,
    }


# ===========================================================================
# Section 1 — Resolver priority order
# ===========================================================================


class TestResolverExplicit:
    """1a. explicit parameter wins regardless of everything else."""

    def test_explicit_beats_yaml(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """explicit overrides the project's own UI.yaml port."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        result = core.resolve_web_port(cfg, "MyApp", explicit=9999)
        assert result["port"] == 9999
        assert result["source"] == "explicit"

    def test_explicit_beats_env_override(
        self, cfg: core.Config, projects_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "7777")
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        result = core.resolve_web_port(cfg, "MyApp", explicit=9999)
        assert result["port"] == 9999
        assert result["source"] == "explicit"

    def test_explicit_not_cached(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """explicit results are ephemeral and must not pollute the TTL cache."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        r1 = core.resolve_web_port(cfg, "MyApp", explicit=9999)
        r2 = core.resolve_web_port(cfg, "MyApp")
        assert r1["source"] == "explicit"
        # Second call (without explicit) must re-resolve from project YAML
        assert r2["source"] == "project"
        assert r2["port"] == 8088


class TestResolverBridgePath:
    """1b. Project port via armed bridge (step 2a)."""

    def test_bridge_port_wins_over_yaml(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When bridge is armed, its port beats the offline YAML."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        bridge_node = _make_bridge_node(port=9001)

        # Bridge is available
        monkeypatch.setattr(
            core, "_bridge_cfg_for", lambda c, p: dataclasses.replace(c)
        )
        monkeypatch.setattr(
            core, "describe_node",
            lambda c, p, path: bridge_node,
        )

        result = core.resolve_web_port(cfg, "MyApp")
        assert result["port"] == 9001
        assert result["source"] == "project"
        assert "bridge" in result["detail"]

    def test_bridge_node_not_found_returns_no_engine(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """NodeNotFound from bridge → source='no_engine'."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)

        monkeypatch.setattr(
            core, "_bridge_cfg_for", lambda c, p: dataclasses.replace(c)
        )
        monkeypatch.setattr(
            core, "describe_node",
            lambda c, p, path: (_ for _ in ()).throw(
                core.NodeNotFound("no node")),
        )

        result = core.resolve_web_port(cfg, "MyApp")
        assert result["source"] == "no_engine"
        assert result["port"] is None

    def test_bridge_error_falls_through_to_yaml(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A general bridge Exception falls through to the offline YAML path."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)

        monkeypatch.setattr(
            core, "_bridge_cfg_for", lambda c, p: dataclasses.replace(c)
        )
        monkeypatch.setattr(
            core, "describe_node",
            lambda c, p, path: (_ for _ in ()).throw(
                core.BridgeUnavailable("bridge down")),
        )

        result = core.resolve_web_port(cfg, "MyApp")
        # Falls back to YAML parse
        assert result["port"] == 8088
        assert result["source"] == "project"


class TestResolverYamlPath:
    """1c. Project port via offline Nodes/UI/UI.yaml parse (step 2b)."""

    def test_yaml_port_returned(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        result = core.resolve_web_port(cfg, "MyApp")
        assert result["port"] == 8088
        assert result["source"] == "project"
        assert "file:" in result["detail"]

    def test_yaml_port_different_value(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        _make_project_with_engine(projects_root, "MyApp", port=4567)
        result = core.resolve_web_port(cfg, "MyApp")
        assert result["port"] == 4567
        assert result["source"] == "project"


class TestResolverNoEngine:
    """1d. Project confirmed to have no WebPresentationEngine (no_engine case)."""

    def test_no_engine_in_yaml_returns_no_engine(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        _make_project_no_engine(projects_root, "NoEngine")
        result = core.resolve_web_port(cfg, "NoEngine")
        assert result["source"] == "no_engine"
        assert result["port"] is None

    def test_no_engine_detail_mentions_ensure_web_engine(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        _make_project_no_engine(projects_root, "NoEngine")
        result = core.resolve_web_port(cfg, "NoEngine")
        assert "optix_bridge_ensure_web_engine" in result["detail"]

    def test_no_ui_yaml_falls_through_to_env_override(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Project with NO Nodes/UI/UI.yaml at all — resolver falls through."""
        proj = projects_root / "Bare"
        proj.mkdir()
        (proj / "Bare.optix").write_text("fake")
        monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "7777")

        result = core.resolve_web_port(cfg, "Bare")
        assert result["source"] == "env_override"
        assert result["port"] == 7777


class TestResolverEnvOverride:
    """1e. OPTIX_RUNTIME_TEST_PORT env-override path (step 3)."""

    def test_env_override_used_when_no_project_port(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No project file → env-override wins."""
        # Project has no UI.yaml, so falls through to env
        proj = projects_root / "Bare"
        proj.mkdir()
        (proj / "Bare.optix").write_text("fake")
        monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "9002")

        result = core.resolve_web_port(cfg, "Bare")
        assert result["source"] == "env_override"
        assert result["port"] == 9002
        assert "OPTIX_RUNTIME_TEST_PORT=9002" in result["detail"]

    def test_env_override_yields_to_yaml_when_project_has_engine(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """UI.yaml with a WebPresentationEngine node beats env-override."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "9002")

        result = core.resolve_web_port(cfg, "MyApp")
        assert result["source"] == "project"
        assert result["port"] == 8088


class TestResolverDefault:
    """1f. Hard default (step 4) — 8081 when nothing else is configured."""

    def test_default_port_returned(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        proj = projects_root / "Bare"
        proj.mkdir()
        (proj / "Bare.optix").write_text("fake")
        # no UI.yaml, no env var

        result = core.resolve_web_port(cfg, "Bare")
        assert result["source"] == "default"
        assert result["port"] == 8081

    def test_default_detail_is_informative(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        proj = projects_root / "Bare"
        proj.mkdir()
        (proj / "Bare.optix").write_text("fake")

        result = core.resolve_web_port(cfg, "Bare")
        assert "8081" in result["detail"]


class TestResolverCacheInvalidation:
    """1g. Cache is invalidated by _invalidate_web_port_cache (called on emulator start)."""

    def test_cache_invalidation_causes_re_resolve(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """After _invalidate_web_port_cache the resolver reads the updated YAML."""
        proj = _make_project_with_engine(projects_root, "MyApp", port=8088)

        r1 = core.resolve_web_port(cfg, "MyApp")
        assert r1["port"] == 8088  # fills cache

        # Simulate project port change
        ui_yaml = proj / "Nodes" / "UI" / "UI.yaml"
        ui_yaml.write_text(
            "- Name: WebPresentationEngine\n"
            "  Type: FTOptix.WebPresentationEngine.WebPresentationEngine\n"
            "  Port: 9100\n",
            encoding="utf-8",
        )

        # Without invalidation, old value still served from TTL cache
        r_stale = core.resolve_web_port(cfg, "MyApp")
        assert r_stale["port"] == 8088  # still cached

        # Invalidate (as run_emulator does)
        core._invalidate_web_port_cache("MyApp")

        r_fresh = core.resolve_web_port(cfg, "MyApp")
        assert r_fresh["port"] == 9100

    def test_reset_web_port_cache_clears_all_projects(
        self, cfg: core.Config, projects_root: Path
    ) -> None:
        """reset_web_port_cache() (test hook) wipes every cached entry."""
        _make_project_with_engine(projects_root, "Alpha", port=8088)
        _make_project_with_engine(projects_root, "Beta", port=8090)

        core.resolve_web_port(cfg, "Alpha")
        core.resolve_web_port(cfg, "Beta")
        assert len(core._WEB_PORT_CACHE) == 2

        core.reset_web_port_cache()
        assert len(core._WEB_PORT_CACHE) == 0


# ===========================================================================
# Section 2 — Self-heal path (_maybe_follow_port)
# ===========================================================================


class TestSelfHeal:
    """_maybe_follow_port: dead expected port + live emulator → re-resolve."""

    def test_returns_none_when_project_is_none(
        self, cfg: core.Config
    ) -> None:
        assert core._maybe_follow_port(cfg, None) is None

    def test_returns_none_when_attach_mode(
        self,
        cfg: core.Config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attach mode means an external runtime URL is configured; port
        following doesn't apply."""
        attach_cfg = dataclasses.replace(cfg, runtime_url="http://remote.host:8080/")
        assert core._maybe_follow_port(attach_cfg, "MyApp") is None

    def test_returns_none_when_expected_port_is_alive(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the expected port answers, no healing is needed."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        monkeypatch.setattr(core, "_tcp_probe", lambda host, port, **kw: True)

        result = core._maybe_follow_port(cfg, "MyApp")
        assert result is None

    def test_returns_none_when_emulator_dead(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Expected port dead + emulator not running → no follow."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        monkeypatch.setattr(core, "_tcp_probe", lambda host, port, **kw: False)
        monkeypatch.setattr(core, "_emulator_pids", lambda: [])

        result = core._maybe_follow_port(cfg, "MyApp")
        assert result is None

    def test_follows_when_dead_expected_but_live_emulator_on_different_port(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The main self-heal scenario: expected port dead, emulator alive,
        fresh resolve finds a different port that IS alive → follow it.

        Strategy: seed the resolver cache with port=8088, then update the
        on-disk YAML to port=9100 so that after _invalidate_web_port_cache
        the re-resolve returns the new value without any mocking of
        resolve_web_port itself.
        """
        proj = _make_project_with_engine(projects_root, "MyApp", port=8088)

        # Seed the TTL cache so _project_web_port returns 8088 first
        core.resolve_web_port(cfg, "MyApp")
        assert core._WEB_PORT_CACHE.get("MyApp") is not None

        # Update the YAML to the new port before _maybe_follow_port runs
        ui_yaml = proj / "Nodes" / "UI" / "UI.yaml"
        ui_yaml.write_text(
            "- Name: WebPresentationEngine\n"
            "  Type: FTOptix.WebPresentationEngine.WebPresentationEngine\n"
            "  Port: 9100\n",
            encoding="utf-8",
        )

        tcp_responses: dict[int, bool] = {8088: False, 9100: True}
        monkeypatch.setattr(
            core, "_tcp_probe",
            lambda host, port, **kw: tcp_responses.get(port, False),
        )
        monkeypatch.setattr(core, "_emulator_pids", lambda: [12345])

        result = core._maybe_follow_port(cfg, "MyApp")
        assert result is not None
        assert result["expected"] == 8088
        assert result["actual"] == 9100
        assert result["project"] == "MyApp"

    def test_returns_none_when_fresh_port_also_dead(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fresh resolve found a port but it's also dead — no follow."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)

        fresh_result = {"port": 9100, "source": "project", "detail": "..."}
        monkeypatch.setattr(core, "_tcp_probe", lambda host, port, **kw: False)
        monkeypatch.setattr(core, "_emulator_pids", lambda: [12345])
        monkeypatch.setattr(core, "resolve_web_port", lambda c, p: fresh_result)

        result = core._maybe_follow_port(cfg, "MyApp")
        assert result is None

    def test_returns_none_when_fresh_port_same_as_expected(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-resolve returns the same port (8088) that was dead — no follow."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)

        fresh_result = {"port": 8088, "source": "project", "detail": "same"}
        tcp_responses: dict[int, bool] = {8088: False}
        monkeypatch.setattr(
            core, "_tcp_probe",
            lambda host, port, **kw: tcp_responses.get(port, True),
        )
        monkeypatch.setattr(core, "_emulator_pids", lambda: [12345])
        monkeypatch.setattr(core, "resolve_web_port", lambda c, p: fresh_result)

        result = core._maybe_follow_port(cfg, "MyApp")
        assert result is None

    def test_cache_invalidated_before_fresh_resolve(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_maybe_follow_port MUST call _invalidate_web_port_cache before
        re-resolving so a stale cached port is not returned as 'fresh'."""
        proj = _make_project_with_engine(projects_root, "MyApp", port=8088)

        # Seed the cache
        core.resolve_web_port(cfg, "MyApp")

        # Update YAML so after invalidation the re-resolve returns 9100
        ui_yaml = proj / "Nodes" / "UI" / "UI.yaml"
        ui_yaml.write_text(
            "- Name: WebPresentationEngine\n"
            "  Type: FTOptix.WebPresentationEngine.WebPresentationEngine\n"
            "  Port: 9100\n",
            encoding="utf-8",
        )

        invalidated: list[str] = []
        original_invalidate = core._invalidate_web_port_cache

        def tracking_invalidate(project: str) -> None:
            invalidated.append(project)
            original_invalidate(project)

        monkeypatch.setattr(core, "_invalidate_web_port_cache", tracking_invalidate)

        tcp_responses: dict[int, bool] = {8088: False, 9100: True}
        monkeypatch.setattr(
            core, "_tcp_probe",
            lambda host, port, **kw: tcp_responses.get(port, False),
        )
        monkeypatch.setattr(core, "_emulator_pids", lambda: [12345])

        core._maybe_follow_port(cfg, "MyApp")
        assert "MyApp" in invalidated


# ===========================================================================
# Section 3 — Doctor row shape
# ===========================================================================

# The autouse _no_bridge and _no_psutil fixtures from test_doctor.py are not
# imported here; instead we patch the relevant functions directly per test.


@pytest.fixture(autouse=True)
def _no_bridge_http(monkeypatch: pytest.MonkeyPatch):
    """Isolate doctor from any live Studio bridge."""
    core.reset_bridge_cache()

    def _down(cfg, path, method="GET", timeout=5.0, **_kw):
        raise core.BridgeUnavailable("no bridge in test")

    monkeypatch.setattr(core, "_bridge_http", _down)
    yield
    core.reset_bridge_cache()


@pytest.fixture(autouse=True)
def _no_port_listener(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(core, "_port_listener", lambda port: None)


@pytest.fixture(autouse=True)
def _no_scan_bind_fail(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(core, "_scan_bind_fail", lambda project: None)


class TestDoctorWebPortRowShape:
    """3a. web_port row structure including squatter pid."""

    def test_web_port_row_present_when_project_given(
        self,
        cfg: core.Config,
        projects_root: Path,
    ) -> None:
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        out = core.doctor(cfg, project="MyApp")
        names = {c["name"] for c in out["checks"]}
        assert "web_port" in names

    def test_web_port_row_absent_without_project(
        self, cfg: core.Config
    ) -> None:
        out = core.doctor(cfg)
        assert all(c["name"] != "web_port" for c in out["checks"])

    def test_web_port_three_fields_present(
        self,
        cfg: core.Config,
        projects_root: Path,
    ) -> None:
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        out = core.doctor(cfg, project="MyApp")
        by = {c["name"]: c for c in out["checks"]}
        wp = by["web_port"]
        assert "project_says" in wp
        assert "service_expects" in wp
        assert "listening" in wp

    def test_web_port_squatter_pid_in_listening_dict(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """listening dict carries the squatter's pid so operators can kill it."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        squatter = {"pid": 4242, "name": "SomeApp.exe"}
        monkeypatch.setattr(core, "_port_listener", lambda port: squatter)

        out = core.doctor(cfg, project="MyApp")
        by = {c["name"]: c for c in out["checks"]}
        wp = by["web_port"]
        assert wp["ok"] is False
        assert wp["listening"] == squatter
        assert wp["listening"]["pid"] == 4242

    def test_web_port_squatter_fix_names_process_and_pid(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fix text must name the squatter process name AND pid number."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        monkeypatch.setattr(
            core, "_port_listener",
            lambda port: {"pid": 4242, "name": "UnknownSquatter.exe"},
        )

        out = core.doctor(cfg, project="MyApp")
        by = {c["name"]: c for c in out["checks"]}
        fix = by["web_port"]["fix"]
        assert "UnknownSquatter.exe" in fix
        assert "4242" in fix

    def test_web_port_is_not_required(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """web_port is informational (required=False); a squatter must not flip
        the overall doctor ready=False."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        monkeypatch.setattr(
            core, "_port_listener",
            lambda port: {"pid": 4242, "name": "Squatter.exe"},
        )

        out = core.doctor(cfg, project="MyApp")
        wp = next(c for c in out["checks"] if c["name"] == "web_port")
        assert wp["required"] is False
        assert out["ready"] is True


# Preserve the real _scan_bind_fail BEFORE any autouse stubs overwrite it.
_REAL_SCAN_BIND_FAIL = core._scan_bind_fail


def _write_bind_fail_log(root: Path, project: str, lines: list[str]) -> Path:
    """Write a fake FTOptixRuntime log under root/project/."""
    d = root / project
    d.mkdir(exist_ok=True)
    p = d / "FTOptixRuntime.0.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture()
def log_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary log root that _scan_bind_fail will read from."""
    root = tmp_path / "emulog"
    root.mkdir()
    monkeypatch.setenv("OPTIX_EMULATOR_LOG_ROOT", str(root))
    return root


class TestDoctorBindFailFromLogFixture:
    """3b. bind-fail signature correctly parsed from a runtime-log fixture."""

    def test_eaddrinuse_signature_parsed(self, log_root: Path) -> None:
        _write_bind_fail_log(log_root, "Alpha", [
            "INFO startup",
            "ERROR EADDRINUSE :8088 - port already bound",
        ])
        result = _REAL_SCAN_BIND_FAIL("Alpha")
        assert result is not None
        assert result["port"] == 8088
        assert "EADDRINUSE" in result["holder"]

    def test_address_already_in_use_signature_parsed(self, log_root: Path) -> None:
        _write_bind_fail_log(log_root, "Beta", [
            "INFO startup",
            "Failed to bind to :9090 — Address already in use",
        ])
        result = _REAL_SCAN_BIND_FAIL("Beta")
        assert result is not None
        assert result["port"] == 9090

    def test_failed_to_bind_socket_signature_parsed(self, log_root: Path) -> None:
        _write_bind_fail_log(log_root, "Gamma", [
            "ERROR Failed to bind socket on port :8081",
        ])
        result = _REAL_SCAN_BIND_FAIL("Gamma")
        assert result is not None
        assert result["port"] == 8081

    def test_no_fail_in_log_returns_none(self, log_root: Path) -> None:
        _write_bind_fail_log(log_root, "Clean", [
            "INFO startup ok",
            "INFO listening on 8088",
        ])
        result = _REAL_SCAN_BIND_FAIL("Clean")
        assert result is None

    def test_missing_log_returns_none(self, log_root: Path) -> None:
        # "Ghost" project has no log directory
        result = _REAL_SCAN_BIND_FAIL("Ghost")
        assert result is None

    def test_most_recent_line_wins(self, log_root: Path) -> None:
        """_scan_bind_fail reverses the log to find the most recent failure."""
        _write_bind_fail_log(log_root, "Delta", [
            "ERROR EADDRINUSE :8080",
            "INFO restart",
            "ERROR EADDRINUSE :8082",
        ])
        result = _REAL_SCAN_BIND_FAIL("Delta")
        assert result is not None
        assert result["port"] == 8082

    def test_bind_fail_web_engine_row_present_when_detected(
        self,
        cfg: core.Config,
        projects_root: Path,
        log_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When _scan_bind_fail returns a match, doctor emits a
        web_engine_bind_failed check row."""
        _make_project_with_engine(projects_root, "MyApp", port=8088)
        _write_bind_fail_log(log_root, "MyApp", [
            "ERROR EADDRINUSE :8088 - port already bound",
        ])
        monkeypatch.setattr(core, "_scan_bind_fail", _REAL_SCAN_BIND_FAIL)

        out = core.doctor(cfg, project="MyApp")
        by = {c["name"]: c for c in out["checks"]}
        assert "web_engine_bind_failed" in by
        row = by["web_engine_bind_failed"]
        assert row["ok"] is False
        assert row["port"] == 8088


# ===========================================================================
# Section 4 — Regression: env-override path still honoured
# ===========================================================================


class TestEnvOverrideRegression:
    """Regression guard: monkeypatching cfg.runtime_test_port must not break
    the env-override path.  Any caller that does::

        cfg = dataclasses.replace(cfg, runtime_test_port=9999)

    should still get source='env_override' (or 'default') when there is no
    project YAML port — i.e. the resolver's env-override path reads the OS
    environment, while cfg.runtime_test_port is a pre-parsed carrier.
    """

    def test_runtime_test_port_monkeypatch_does_not_suppress_env_override(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Monkeypatching cfg.runtime_test_port to a custom value AND setting
        OPTIX_RUNTIME_TEST_PORT: the resolver's env-override step reads the
        ENV VAR, not cfg.runtime_test_port, so the env var value wins."""
        proj = projects_root / "Bare"
        proj.mkdir()
        (proj / "Bare.optix").write_text("fake")

        monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "7654")
        patched_cfg = dataclasses.replace(cfg, runtime_test_port=9876)

        result = core.resolve_web_port(patched_cfg, "Bare")
        assert result["source"] == "env_override"
        assert result["port"] == 7654

    def test_cfg_runtime_test_port_still_used_as_probe_fallback(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no project port and no env var, resolve_web_port falls to the
        4-step default (8081), while cfg.runtime_test_port is still honoured
        by runtime_probe_port() for probe calls — they're separate concerns."""
        proj = projects_root / "Bare"
        proj.mkdir()
        (proj / "Bare.optix").write_text("fake")

        # No env var; resolver falls to default 8081
        result = core.resolve_web_port(cfg, "Bare")
        assert result["source"] == "default"
        assert result["port"] == 8081

        # runtime_probe_port returns cfg.runtime_test_port (8081 default)
        assert core.runtime_probe_port(cfg) == cfg.runtime_test_port

    def test_existing_monkeypatched_cfg_runtime_test_port_path_passes(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: tests that used cfg.runtime_test_port = X to pin the
        runtime port relied on Config.from_env parsing OPTIX_RUNTIME_TEST_PORT.
        This confirms the env-override path still picks it up."""
        proj = projects_root / "Bare"
        proj.mkdir()
        (proj / "Bare.optix").write_text("fake")

        # Simulate how existing tests set up the env-override:
        monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "5555")
        cfg_with_env = core.Config.from_env()

        # resolve_web_port must use the env var (env_override step)
        result = core.resolve_web_port(cfg_with_env, "Bare")
        assert result["source"] == "env_override"
        assert result["port"] == 5555

        # cfg.runtime_test_port is also consistent (from_env parsed same value)
        assert cfg_with_env.runtime_test_port == 5555

    def test_env_override_source_string_is_env_override_not_default(
        self,
        cfg: core.Config,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ensure source label is 'env_override', not 'default' or 'project'.
        Callers that check `result["source"] == "env_override"` must not break."""
        proj = projects_root / "Bare"
        proj.mkdir()
        (proj / "Bare.optix").write_text("fake")
        monkeypatch.setenv("OPTIX_RUNTIME_TEST_PORT", "9999")

        result = core.resolve_web_port(cfg, "Bare")
        assert result["source"] == "env_override"
        assert result["source"] != "default"
