<#
.SYNOPSIS
    Lifecycle helper for ftx-mcp scheduled tasks.

.DESCRIPTION
    One-arg wrapper around Start/Stop/Get-ScheduledTask for the two
    tasks that make up a full install:

        ftx-mcp             main service (HTTP :8765, MCP :8766)
        ftx-mcp-chrome-cdp  Chrome CDP launcher for canvas verify (optional)

    Tasks that don't exist (e.g. chrome-cdp was skipped at install time)
    are reported as "skip" and never errored on. Status action probes
    /health to disambiguate "auth required (401)" from "service down".

.PARAMETER Action
    One of: start | stop | restart | status | enable-autostart | disable-autostart.
    start    - Start-ScheduledTask on each registered task.
    stop     - Stop-ScheduledTask on each registered task.
    restart  - stop, sleep 1s, then start.
    status   - report State, LastRunTime, LastTaskResult, port-listen,
               and a single /health probe at the end.
    enable-autostart  - add an at-logon trigger so the service starts
                        automatically after a reboot (opt-in).
    disable-autostart - remove triggers; back to manual start (the default).

.EXAMPLE
    .\bootstrap\services.ps1 start

.EXAMPLE
    .\bootstrap\services.ps1 status
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet("start", "stop", "restart", "status", "enable-autostart", "disable-autostart")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Each entry: TaskName + the port the task is expected to bind (used by
# status to TCP-probe presence). 0 means "no port to probe".
$tasks = @(
    @{ Name = "ftx-mcp";            Port = 8766 },
    @{ Name = "ftx-mcp-chrome-cdp"; Port = 9222 }
)

function Get-TaskOrNull($name) {
    Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
}

function Test-Port($port) {
    if ($port -le 0) { return $false }
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    [bool]$c
}

# The CDP chrome is identified by its dedicated profile dir, NOT just the
# port: a user's own browser could legitimately hold a debug port, and a
# reinstall (Unregister + Register in install-chrome-cdp.ps1) ORPHANS any
# chrome the OLD task spawned - Stop-ScheduledTask on the new task then
# does nothing while the orphan keeps 9222. Field report 2026-07-22
# ("cdp has its own mind"). Match on the profile-dir marker so we only
# ever touch our chrome.
$cdpProfileMarker = Join-Path $env:LOCALAPPDATA "ftx-mcp\chrome-cdp-profile"

function Get-CdpChromePids($port) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { return @() }
    $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
    $ours = @()
    foreach ($procId in $pids) {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue).CommandLine
        if ($cmd -and $cmd -like "*$cdpProfileMarker*") { $ours += $procId }
    }
    return $ours
}

function Do-Start($name, $port) {
    # Returns $true when the task was actually started (caller prints the
    # start line); $false when it early-returned with its own message.
    if ($name -eq "ftx-mcp-chrome-cdp") {
        $ours = @(Get-CdpChromePids $port)
        if ($ours.Count -gt 0) {
            Write-Host ("  ok    {0,-28} (cdp chrome already on :{1}, pid {2})" -f $name, $port, ($ours -join ",")) -ForegroundColor Green
            return $false
        }
        $other = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($other) {
            $holder = ($other | Select-Object -First 1).OwningProcess
            Write-Host ("  WARN  {0,-28} :{1} held by pid {2} (NOT the ftx-mcp cdp chrome) - not starting" -f $name, $port, $holder) -ForegroundColor Yellow
            return $false
        }
    }
    Start-ScheduledTask -TaskName $name
    return $true
}

function Do-Stop($name, $port) {
    # Best-effort: a Stop on a task that's already Ready is a no-op error
    # in some PS versions. SilentlyContinue keeps the loop moving.
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($name -eq "ftx-mcp-chrome-cdp") {
        # Reap our chrome even when the task no longer owns it (orphan case).
        foreach ($procId in @(Get-CdpChromePids $port)) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Write-Host ("  kill  {0,-28} (orphan cdp chrome pid {1} on :{2})" -f $name, $procId, $port) -ForegroundColor Yellow
        }
    }
}

foreach ($t in $tasks) {
    $name = $t.Name
    $task = Get-TaskOrNull $name
    if (-not $task) {
        Write-Host ("  skip  {0,-28} (not registered)" -f $name) -ForegroundColor DarkGray
        continue
    }
    switch ($Action) {
        "start" {
            if (Do-Start $name $t.Port) {
                Write-Host ("  start {0,-28}" -f $name) -ForegroundColor Green
            }
        }
        "stop" {
            Do-Stop $name $t.Port
            Write-Host ("  stop  {0,-28}" -f $name) -ForegroundColor Yellow
        }
        "restart" {
            Do-Stop $name $t.Port
            Start-Sleep -Seconds 1
            if (Do-Start $name $t.Port) {
                Write-Host ("  rstrt {0,-28}" -f $name) -ForegroundColor Green
            }
        }
        "enable-autostart" {
            # Opt-in: add an at-logon trigger so the service comes up in the
            # interactive session after a reboot. Default (no trigger) stays manual.
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
            Set-ScheduledTask -TaskName $name -Trigger $trigger | Out-Null
            Write-Host ("  auto+ {0,-28} (starts at logon)" -f $name) -ForegroundColor Green
        }
        "disable-autostart" {
            # Back to manual: clear all triggers.
            $task.Triggers = @()
            $task | Set-ScheduledTask | Out-Null
            Write-Host ("  auto- {0,-28} (manual only)" -f $name) -ForegroundColor Yellow
        }
        "status" {
            $info = Get-ScheduledTaskInfo -TaskName $name
            $state = $task.State
            $port = $t.Port
            $listening = Test-Port $port
            $portMark = if ($listening) { "LISTEN" } else { "----- " }
            $lastRun = if ($info.LastRunTime) { $info.LastRunTime.ToString("yyyy-MM-dd HH:mm") } else { "never           " }
            $result = "0x{0:x8}" -f $info.LastTaskResult
            $line = "  {0,-28} state={1,-8} last={2} result={3} port:{4,-5} {5}" -f `
                $name, $state, $lastRun, $result, $port, $portMark
            Write-Host $line
        }
    }
}

if ($Action -eq "status") {
    Write-Host ""
    # /health probe (unauthenticated). The three outcomes worth distinguishing:
    #   200 -> service up, auth disabled (or somehow not required for /health)
    #   401 -> service up, auth required (bearer opt-in enabled)
    #   conn refused / timeout -> service down
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8765/health" `
            -TimeoutSec 2 -UseBasicParsing
        $body = $r.Content | ConvertFrom-Json
        Write-Host ("  /health 200 (auth disabled), version={0}" -f $body.version) -ForegroundColor Green
    } catch {
        $resp = $_.Exception.Response
        if ($resp -and $resp.StatusCode.value__ -eq 401) {
            Write-Host "  /health 401 (service running, auth required)" -ForegroundColor Green
        } elseif ($_.Exception.Message -match "actively refused|Unable to connect|timed out") {
            Write-Host "  /health down (no listener on :8765)" -ForegroundColor Red
        } else {
            $first = ($_.Exception.Message -split "`n")[0]
            Write-Host ("  /health err ({0})" -f $first) -ForegroundColor Red
        }
    }
}
