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
               Add -Silent to start the service with its console hidden
               (rewrites the task action to pass --hide-console). A plain
               start clears it again, so the verb you use is the behavior
               you get -- no sticky state.
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
    [string]$Action,
    # Applies to start/restart. Rewrites the ftx-mcp task action to pass (or
    # NOT pass) --hide-console, then starts it -- so the SAME verb controls
    # console visibility and the task definition always tells the truth about
    # how the service was last started. Deterministic in both directions: a
    # plain start clears the flag, -Silent sets it.
    [switch]$Silent
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "_common.ps1")

function Set-ServiceConsoleMode {
    # The scheduled task always launches console-subsystem python.exe (there is
    # deliberately no second, windowless launcher to validate). --hide-console
    # asks the service to hide the console it already owns, which keeps real
    # stdout/stderr and leaves a console for child processes to inherit.
    param([bool]$Hide)
    $t = Get-ScheduledTask -TaskName $Script:FtxTaskName -ErrorAction SilentlyContinue
    if (-not $t) { return }
    $exe = $t.Actions[0].Execute
    $wd  = $t.Actions[0].WorkingDirectory
    $want = if ($Hide) { "-m service --hide-console" } else { "-m service" }
    if ($t.Actions[0].Arguments -eq $want) { return $false }   # already correct
    $a = New-ScheduledTaskAction -Execute $exe -Argument $want -WorkingDirectory $wd
    try {
        Set-ScheduledTask -TaskName $Script:FtxTaskName -Action $a | Out-Null
    } catch {
        # I27: if setup.ps1 was run elevated, the task is Admins-owned and this
        # rewrite dies here with a raw CIM "Access is denied" (0x80070005) --
        # while plain `start` (no console-mode change) works fine, so the
        # failure reads as "-Silent is broken" instead of "wrong shell".
        # Under PS 5.1, Set-ScheduledTask surfaces this as a
        # Microsoft.Management.Infrastructure.CimException (sometimes wrapped
        # in UnauthorizedAccessException); match by HRESULT first, message as
        # a fallback, so we don't misclassify an unrelated failure.
        $ex = $_.Exception
        $hr = $ex.HResult
        $innerHr = if ($ex.InnerException) { $ex.InnerException.HResult } else { $null }
        $isAccessDenied = ($hr -eq -2147024891) -or ($innerHr -eq -2147024891) -or
            ($ex -is [System.UnauthorizedAccessException]) -or
            ($ex.Message -match "Access is denied") -or
            ($ex.Message -match "0x80070005")
        if (-not $isAccessDenied) { throw }
        Write-Host ""
        Write-Host "The ftx-mcp scheduled task was registered from an elevated shell," -ForegroundColor Yellow
        Write-Host "so console-mode changes need elevation too." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "Fix with ONE of:" -ForegroundColor Yellow
        Write-Host "    Run 'services.ps1 start -Silent' once from an elevated shell" -ForegroundColor Cyan
        Write-Host "    Uninstall (elevated), then re-run setup.ps1 from a regular shell" -ForegroundColor Cyan
        Write-Host "    to re-register the task non-elevated" -ForegroundColor Cyan
        Write-Host ""
        Fail "cannot change console mode -- Access is denied (0x80070005)."
    }
    Write-Host ("  mode  {0,-28} {1}" -f $Script:FtxTaskName,
        $(if ($Hide) { "console hidden" } else { "console visible" })) -ForegroundColor DarkCyan
    return $true
}

# Each entry: TaskName + the port the task is expected to bind (used by
# status to TCP-probe presence). 0 means "no port to probe".
$tasks = @(
    @{ Name = $Script:FtxTaskName;    Port = $Script:FtxMcpPort },
    @{ Name = $Script:FtxCdpTaskName; Port = $Script:FtxCdpPort }
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
        # Dead-pid window (issue #1, reported by @Jraa01): the listening
        # socket outlives its process by a beat. On a restart, Do-Stop kills
        # our chrome and Do-Start re-probes ~1s later while :9222 is still
        # LISTEN with OwningProcess naming a pid that is already gone.
        # Get-CimInstance then returns nothing, and under Set-StrictMode
        # dotting .CommandLine off $null is a TERMINATING error that
        # ErrorActionPreference=Stop propagates out of the script. Bind
        # first, guard, then read. Select-Object -First 1 also normalizes
        # the pid-reuse case where the filter can match more than one.
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue |
                Select-Object -First 1
        if (-not $proc) { continue }
        $cmd = $proc.CommandLine
        if ($cmd -and $cmd -like "*$cdpProfileMarker*") { $ours += $procId }
    }
    return $ours
}

function Do-Start($name, $port) {
    # Returns $true when the task was actually started (caller prints the
    # start line); $false when it early-returned with its own message.
    if ($name -eq $Script:FtxCdpTaskName) {
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
    if ($name -eq $Script:FtxCdpTaskName) {
        # Reap our chrome even when the task no longer owns it (orphan case).
        foreach ($procId in @(Get-CdpChromePids $port)) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Write-Host ("  kill  {0,-28} (orphan cdp chrome pid {1} on :{2})" -f $name, $procId, $port) -ForegroundColor Yellow
        }
    }
}

$hasFault = $false

foreach ($t in $tasks) {
    $name = $t.Name
    $task = Get-TaskOrNull $name
    if (-not $task) {
        Write-Host ("  skip  {0,-28} (not registered)" -f $name) -ForegroundColor DarkGray
        continue
    }
    if ($name -eq $Script:FtxTaskName -and $Action -in @("start","restart")) {
        $modeChanged = Set-ServiceConsoleMode -Hide:$Silent.IsPresent
        # A task action only takes effect on the NEXT launch, and
        # Start-ScheduledTask is a no-op while the task is already Running --
        # so a bare `start -Silent` against a live service would rewrite the
        # definition and change nothing the operator can see. Recycle it.
        if ($modeChanged -and $Action -eq "start" -and
            (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue).State -eq "Running") {
            Write-Host ("  recyc {0,-28} (console mode changed while running)" -f $name) -ForegroundColor DarkCyan
            Do-Stop $name $t.Port
            Start-Sleep -Seconds 1
        }
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
            # Running-but-port-dead: process is live but its port is not
            # listening -- the one observable signature of a crash-loop.
            # Emit a prominent FAULT line and arrange a non-zero exit.
            if ($state -eq "Running" -and $port -gt 0 -and -not $listening) {
                $logPath = Join-Path $env:LOCALAPPDATA "ftx-mcp\logs\service.jsonl"
                $lastEvtSummary = "(service.jsonl not found)"
                if (Test-Path $logPath) {
                    $lastLine = (Get-Content $logPath -ErrorAction SilentlyContinue |
                                 Where-Object { $_.Trim() } | Select-Object -Last 1)
                    if ($lastLine) {
                        try {
                            $evt = $lastLine | ConvertFrom-Json
                            $lastEvtSummary = "event={0} ts={1} pid={2}" -f $evt.event, $evt.ts, $evt.pid
                            if ($evt.error) {
                                $errFirst = ($evt.error -split "`n")[0]
                                $lastEvtSummary += " error=$errFirst"
                            }
                        } catch {
                            $lastEvtSummary = "(parse error)"
                        }
                    } else {
                        $lastEvtSummary = "(service.jsonl empty)"
                    }
                }
                Write-Host ("  FAULT {0,-28} state=Running port:{1} not listening -- {2}" -f `
                    $name, $port, $lastEvtSummary) -ForegroundColor Red
                $hasFault = $true
            }
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
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$($Script:FtxHttpPort)/health" `
            -TimeoutSec 2 -UseBasicParsing
        $body = $r.Content | ConvertFrom-Json
        Write-Host ("  /health 200 (auth disabled), version={0}" -f $body.version) -ForegroundColor Green
    } catch {
        $resp = $_.Exception.Response
        if ($resp -and $resp.StatusCode.value__ -eq 401) {
            Write-Host "  /health 401 (service running, auth required)" -ForegroundColor Green
        } elseif ($_.Exception.Message -match "actively refused|Unable to connect|timed out") {
            Write-Host ("  /health down (no listener on :{0})" -f $Script:FtxHttpPort) -ForegroundColor Red
        } else {
            $first = ($_.Exception.Message -split "`n")[0]
            Write-Host ("  /health err ({0})" -f $first) -ForegroundColor Red
        }
    }
    if ($hasFault) { exit 1 }
}
