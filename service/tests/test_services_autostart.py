"""Pester-free test: `services.ps1 enable-autostart` starts only ftx-mcp.

Parses the script text (no PowerShell needed, runs on the Linux runner) and
asserts the ``enable-autostart`` branch (a) only registers a trigger for the
ftx-mcp service task, and (b) never references Studio, the emulator or CDP.
"""
from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "bootstrap" / "services.ps1"

FORBIDDEN = re.compile(r"studio|emulator|cdp|chrome|FtxCdp|Start-Process", re.IGNORECASE)


def _autostart_branch() -> str:
    """Return the body of the ``"enable-autostart" { ... }`` switch branch."""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'^\s*"enable-autostart"\s*\{', text, re.MULTILINE)
    assert m, "enable-autostart switch branch not found in services.ps1"
    depth, i = 1, m.end()
    while depth and i < len(text):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    assert depth == 0, "unbalanced braces in enable-autostart branch"
    return text[m.end():i - 1]


def _code_lines(branch: str) -> list[str]:
    """Branch lines with comments and string literals (messages) removed."""
    out = []
    for line in branch.splitlines():
        line = re.sub(r'"[^"]*"', '""', line)
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_autostart_guards_to_ftx_mcp_task_only() -> None:
    code = "\n".join(_code_lines(_autostart_branch()))
    # Guard: any task that is not the ftx-mcp service task is skipped.
    assert re.search(r"if\s*\(\s*\$name\s+-ne\s+\$Script:FtxTaskName\s*\)\s*\{[^}]*\bcontinue\b", code), code


def test_autostart_registers_only_a_logon_trigger_on_the_service_task() -> None:
    code = "\n".join(_code_lines(_autostart_branch()))
    assert code.count("New-ScheduledTaskTrigger") == 1
    assert "-AtLogOn" in code
    # Trigger is applied to the loop's task, which the guard limits to ftx-mcp.
    assert re.search(r"Set-ScheduledTask\s+-TaskName\s+\$name\s+-Trigger\s+\$trigger", code)


def test_autostart_branch_does_not_start_studio_emulator_or_cdp() -> None:
    code = "\n".join(_code_lines(_autostart_branch()))
    assert not FORBIDDEN.search(code), f"autostart branch references a non-service target:\n{code}"
    assert "Start-ScheduledTask" not in code


def test_autostart_detector_fails_when_extra_start_is_added() -> None:
    """Self-check: the forbidden-pattern scan catches an added CDP/Studio start."""
    for extra in (
        "Start-ScheduledTask -TaskName $Script:FtxCdpTaskName",
        "Start-Process $studioExe",
        "Start-ScheduledTask -TaskName $name",
    ):
        code = "\n".join(_code_lines("$trigger = New-ScheduledTaskTrigger -AtLogOn\n" + extra))
        assert FORBIDDEN.search(code) or "Start-ScheduledTask" in code, extra
