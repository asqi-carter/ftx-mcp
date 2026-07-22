<#
.SYNOPSIS
    Tear down an ftx-mcp install (the mirror of setup.ps1).

.DESCRIPTION
    Default action removes the RUNNING pieces so a re-run of setup.ps1 is
    clean: stops + unregisters both scheduled tasks (ftx-mcp,
    ftx-mcp-chrome-cdp) and reaps any CDP chrome left holding :9222 -
    including orphans from a previous install, identified by the dedicated
    chrome-cdp-profile dir (never by port alone).

    State is preserved by default. Opt in to deeper cleaning:

.PARAMETER PurgeState
    Also delete the state dir (%LOCALAPPDATA%\ftx-mcp, or OPTIX_STATE_DIR
    when set): logs, export-staging, runtime, chrome profile AND
    secrets\tokens.json.dpapi (issued bearer tokens are destroyed).
    Also clears the persisted FTX_AUTH_REQUIRED user env var so the next
    setup.ps1 starts from the auth-off default.

.PARAMETER PurgeVenv
    Also delete the repo-local .venv so setup.ps1 rebuilds it.

.PARAMETER All
    PurgeState + PurgeVenv. Full clean-slate; setup.ps1 afterwards is a
    from-scratch install. NOT removed even by -All: the repo itself, and
    any MCP client config written by setup-mcp-client.ps1 (Claude/VS Code
    configs point at ports, not files - they go stale harmlessly and are
    refreshed by re-running setup-mcp-client.ps1).

.EXAMPLE
    .\bootstrap\uninstall.ps1
    .\bootstrap\setup.ps1              # fast reinstall (state/venv kept)

.EXAMPLE
    .\bootstrap\uninstall.ps1 -All
    .\bootstrap\setup.ps1              # true from-scratch install test
#>
[CmdletBinding()]
param(
    [switch]$PurgeState,
    [switch]$PurgeVenv,
    [switch]$All
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Ok($msg)   { Write-Host "ok: $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }
function Section($t){ Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }

if ($All) { $PurgeState = $true; $PurgeVenv = $true }

$RepoRoot = Split-Path -Parent $PSScriptRoot
if ($env:OPTIX_STATE_DIR) { $state = $env:OPTIX_STATE_DIR }
else { $state = Join-Path $env:LOCALAPPDATA "ftx-mcp" }
$venvDir = Join-Path $RepoRoot ".venv"
$cdpMarker = Join-Path $env:LOCALAPPDATA "ftx-mcp\chrome-cdp-profile"

Section "1. Scheduled tasks"
foreach ($name in @("ftx-mcp", "ftx-mcp-chrome-cdp")) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Ok "removed task $name"
    } else {
        Ok "task $name not present"
    }
}

Section "2. CDP chrome reap"
# Orphan-aware: identify OUR chrome by its dedicated profile dir. A user's
# own browser on a debug port is out of bounds.
$conns = Get-NetTCPConnection -LocalPort 9222 -State Listen -ErrorAction SilentlyContinue
$reaped = 0
if ($conns) {
    foreach ($procId in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue).CommandLine
        if ($cmd -and $cmd -like "*$cdpMarker*") {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Ok "killed cdp chrome pid $procId"
            $reaped++
        } else {
            Warn ":9222 held by pid $procId (not ftx-mcp's cdp chrome) - left alone"
        }
    }
}
if ($reaped -eq 0 -and -not $conns) { Ok "no cdp chrome on :9222" }

Section "3. State"
if ($PurgeState) {
    if (Test-Path $state) {
        Remove-Item -Recurse -Force $state
        Ok "deleted state dir $state (incl. issued tokens)"
    } else {
        Ok "state dir $state not present"
    }
    $persistedAuth = [Environment]::GetEnvironmentVariable("FTX_AUTH_REQUIRED", "User")
    if ($persistedAuth) {
        [Environment]::SetEnvironmentVariable("FTX_AUTH_REQUIRED", $null, "User")
        Ok "cleared persisted FTX_AUTH_REQUIRED (was '$persistedAuth')"
    } else {
        Ok "FTX_AUTH_REQUIRED not persisted"
    }
} else {
    Ok "kept state dir $state (use -PurgeState to delete; -All for full clean)"
}

Section "4. Venv"
if ($PurgeVenv) {
    if (Test-Path $venvDir) {
        Remove-Item -Recurse -Force $venvDir
        Ok "deleted $venvDir"
    } else {
        Ok "venv not present"
    }
} else {
    Ok "kept $venvDir (use -PurgeVenv to delete)"
}

Section "Done"
Ok "uninstall complete - re-run bootstrap\setup.ps1 to reinstall"
