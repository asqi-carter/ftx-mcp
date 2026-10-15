"""The unit suite must not depend on what listens on the host (#91)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from service import core

_REPO = Path(__file__).resolve().parents[2]

_AFFECTED = [
    "service/tests/test_run_emulator.py::test_emulator_status_running_needs_pid_and_port",
    "service/tests/test_run_emulator.py::test_emulator_status_running_state_with_log_fixture",
    "service/tests/test_run_emulator.py::test_emulator_status_project_excludes_other_project_pid",
    "service/tests/test_run_emulator.py::test_run_emulator_already_running_sends_no_f5[True-running]",
    "service/tests/test_run_emulator.py::test_emulator_status_port_attribution_when_name_differs_from_cache_folder",
    "service/tests/test_run_emulator.py::test_run_emulator_mismatch_guard_is_case_insensitive",
]

_LIVE_ENV = any(k.startswith("FTX_LIVE_") and v for k, v in os.environ.items())


@pytest.mark.skipif(_LIVE_ENV, reason="FTX_LIVE_* set: the guard is intentionally off")
def test_net_connections_guard_raises_in_unit_tests() -> None:
    """conftest turns a host listener-table read into a loud failure."""
    with pytest.raises(AssertionError, match="listener table"):
        psutil.net_connections(kind="inet")


@pytest.mark.live
def test_net_connections_guard_exempts_live_tests() -> None:
    """A test marked ``live`` keeps the real psutil call (the guard stays out)."""
    try:
        psutil.net_connections(kind="inet")
    except (psutil.Error, OSError):
        pass  # unreadable table on this host is fine; the guard did not fire


def test_net_connections_guard_exempts_ftx_live_env_runs(tmp_path: Path) -> None:
    """A run configured with any FTX_LIVE_* env var (``_is_live_run``) hits no
    HostListenerTableAccess death even for an unmarked test; without it the
    same test dies on the guard."""
    t = tmp_path / "test_probe.py"
    t.write_text(
        "import psutil\n"
        "def test_probe():\n"
        "    try:\n"
        "        psutil.net_connections(kind='inet')\n"
        "    except (psutil.Error, OSError):\n"
        "        pass\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("FTX_LIVE_")}

    def run(extra: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--rootdir", str(_REPO), "-c", str(_REPO / "pyproject.toml"),
             "-p", "service.tests.conftest", str(t)],
            cwd=_REPO, capture_output=True, text=True, timeout=120,
            env={**env, **extra})

    assert run({"FTX_LIVE_PROJECT": "P"}).returncode == 0
    assert run({}).returncode != 0


def test_affected_emulator_tests_pass_with_a_socket_bound_on_the_test_port() -> None:
    """A real listener on the runtime test port (a foreign process in the
    host's table) must not change the five port-dependent emulator tests."""
    port = core.Config.__dataclass_fields__["runtime_test_port"].default
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            srv.bind(("127.0.0.1", int(port)))
        except OSError:
            pytest.skip(f"test port {port} already in use on this host")
        srv.listen(1)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             *_AFFECTED],
            cwd=_REPO, capture_output=True, text=True, timeout=300,
            env={**os.environ, "OPTIX_RUNTIME_TEST_PORT": str(port)})
    finally:
        srv.close()
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
    assert f"{len(_AFFECTED)} passed" in proc.stdout
