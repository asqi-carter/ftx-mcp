"""Static checks (no PowerShell needed) for setup.ps1 step 1 own-service recognition.

Behavioural coverage with the process lookup mocked lives in
tests/bootstrap/setup-port-conflict.Tests.ps1 (Pester); this file guards the
script text on the Linux runner.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP = (ROOT / "bootstrap" / "setup.ps1").read_text(encoding="utf-8")
COMMON = (ROOT / "bootstrap" / "_common.ps1").read_text(encoding="utf-8")
PESTER = (ROOT / "tests" / "bootstrap" / "setup-port-conflict.Tests.ps1").read_text(encoding="utf-8")

STOP_HINT = "ftx-mcp service is running; run ``bootstrap\\services.ps1 stop``, then re-run setup"


def _step1() -> str:
    start = SETUP.index('Section "1. Port-conflict detection"')
    return SETUP[start:SETUP.index('Section "2. Verify FT Optix Studio"')]


def test_own_service_helper_looks_up_process_and_matches_venv_module() -> None:
    m = re.search(r"function Test-FtxOwnServiceProcess \{.*?\n\}\n", COMMON, re.DOTALL)
    assert m, "Test-FtxOwnServiceProcess missing from _common.ps1"
    body = m.group(0)
    assert "Get-CimInstance Win32_Process" in body
    assert ".venv" in body and "-m service" in body
    assert "return $false" in body  # dead pid -> foreign


def test_own_service_branch_prints_stop_hint_between_cdp_and_foreign() -> None:
    step = _step1()
    cdp = step.index("$c.Port -eq $Script:FtxCdpPort")
    own = step.index("Test-FtxOwnServiceProcess")
    foreign = step.index("override with OPTIX_HTTP_PORT")
    assert cdp < own < foreign
    assert STOP_HINT in step
    # Still exits non-zero via the shared Fail at the end of the conflict block.
    assert 'Fail "Resolve port conflicts before continuing."' in step


def test_own_service_branch_is_limited_to_http_and_mcp_ports() -> None:
    step = _step1()
    cond = step[step.index("} elseif ("):step.index("Test-FtxOwnServiceProcess")]
    assert "$httpPort" in cond and "$mcpPort" in cond
    assert "runtimeTestPort" not in cond


def test_step1_never_stops_the_service() -> None:
    code = re.sub(r"#.*", "", _step1())
    for banned in ("Stop-ScheduledTask", "Stop-Process", "Start-Process"):
        assert banned not in code, banned


def test_cdp_logic_and_dead_pid_asymmetry_comment_unchanged() -> None:
    step = _step1()
    assert "deliberate asymmetry" in step
    assert "Do not unify these" in step
    assert "$cmd -like \"*$cdpMarker*\"" in step
    assert "(not ftx-mcp's CDP chrome)" in step


def test_pester_file_covers_own_and_foreign_with_mocked_lookup() -> None:
    assert "Mock Get-CimInstance" in PESTER
    assert "own_service_listener_is_recognised" in PESTER
    assert "foreign_holder_is_not_own_service" in PESTER


def test_own_service_helper_checks_parent_process_one_level() -> None:
    m = re.search(r"function Test-FtxOwnServiceProcess \{.*?\n\}\n", COMMON, re.DOTALL)
    assert m
    body = m.group(0)
    assert "ParentProcessId" in body
    assert body.count("& $lookup") == 2  # listener + exactly one parent lookup


def test_pester_file_covers_venv_launcher_parent_cases() -> None:
    assert "venv_launcher_child_is_recognised_via_parent" in PESTER
    assert "child_of_unrelated_parent_is_not_own_service" in PESTER
    assert "dead_pid_is_treated_as_foreign" in PESTER
