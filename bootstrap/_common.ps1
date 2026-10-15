<#
.SYNOPSIS
    Shared helpers for bootstrap/*.ps1 -- dot-source, do not execute directly.

.DESCRIPTION
    Every top-level bootstrap script dot-sources this file immediately after
    setting $ErrorActionPreference / Set-StrictMode:

        . (Join-Path $PSScriptRoot "_common.ps1")

    It defines the Ok/Warn/Section/Fail console helpers, the ftx-mcp
    task-name/port constants, and the handful of functions that were
    previously duplicated byte-for-byte across setup.ps1,
    install-chrome-cdp.ps1, issue-token.ps1, revoke-token.ps1, services.ps1,
    and uninstall.ps1.

    bootstrap/optional/updatesvc/*.ps1 (a separate, not-yet-wired deploy
    path per setup.ps1's own docstring) is intentionally NOT converted to
    dot-source this file in this pass -- see the PR notes for the scope
    boundary.
#>

# --- Task-name / port constants --------------------------------------------
# Defaults only. setup.ps1's OPTIX_HTTP_PORT / OPTIX_MCP_PORT env-override
# logic (used by install-smoke runs that redirect ports) reads the env var
# first and falls back to these constants -- it keeps taking precedence,
# never the other way around.
$Script:FtxTaskName    = "ftx-mcp"
$Script:FtxCdpTaskName = "ftx-mcp-chrome-cdp"
$Script:FtxHttpPort    = 8765
$Script:FtxMcpPort     = 8766
$Script:FtxCdpPort     = 9222

# --- Console helpers ---------------------------------------------------------
function Section($name) {
    Write-Host ""
    Write-Host "=== $name ===" -ForegroundColor Cyan
}

function Ok($msg) {
    Write-Host "ok: $msg" -ForegroundColor Green
}

function Warn($msg) {
    Write-Host "WARN: $msg" -ForegroundColor Yellow
}

function Fail($msg) {
    Write-Host "FAIL: $msg" -ForegroundColor Red
    exit 1
}

# --- MSIX-packaged-shell guard -----------------------------------------------
function Assert-NotPackagedShell {
    # Refuses to continue when running inside an MSIX-packaged shell (e.g.
    # the Microsoft Store build of Claude Desktop). Writes to %LOCALAPPDATA%
    # from a packaged process are virtualized into the app's private
    # LocalCache overlay: in-shell checks see the merged view and pass, but
    # the ftx-mcp scheduled tasks run OUTSIDE the package against the real
    # filesystem, where none of those writes exist. The service self-creates
    # its state dirs at startup, but token/secret writes have no such
    # recovery, so the only safe behavior is to refuse and point at a
    # regular shell.
    #
    # Existence-guarded: Add-Type -Name/-Namespace defines a new in-memory
    # type and throws if called twice in the same process, which would
    # happen if _common.ps1 is dot-sourced more than once in one PowerShell
    # session.
    if (-not ([System.Management.Automation.PSTypeName]'FtxCommon.PkgIdentity').Type) {
        $pkgSig = @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
public static extern int GetCurrentPackageFullName(ref uint length, System.Text.StringBuilder fullName);
'@
        Add-Type -MemberDefinition $pkgSig -Name PkgIdentity -Namespace FtxCommon
    }
    $pkgLen = [uint32]0
    # 15700 = APPMODEL_ERROR_NO_PACKAGE -> unpackaged process, safe to proceed
    if ([FtxCommon.PkgIdentity]::GetCurrentPackageFullName([ref]$pkgLen, $null) -ne 15700) {
        Fail ("This shell is running inside an MSIX-packaged app (e.g. the Microsoft " +
              "Store build of Claude Desktop). Its writes to %LOCALAPPDATA% are " +
              "virtualized into the app's private LocalCache and invisible to the " +
              "ftx-mcp scheduled tasks. Re-run from a regular PowerShell window.")
    }
}

# --- DPAPI tokens.json.dpapi read/write --------------------------------------
Add-Type -AssemblyName System.Security

function Read-FtxTokensBlob($tokensBlob) {
    # Returns "" (not an error) when the blob doesn't exist yet -- a first
    # install has no tokens to decrypt. Callers that need "file not present
    # yet" to be its own friendlier case (revoke-token.ps1 -List on a fresh
    # box) still do their own Test-Path check before calling this.
    $plaintext = ""
    if (Test-Path $tokensBlob) {
        try {
            $cipher = [System.IO.File]::ReadAllBytes($tokensBlob)
            $bytes  = [System.Security.Cryptography.ProtectedData]::Unprotect(
                $cipher, $null, 'CurrentUser')
            $plaintext = [System.Text.Encoding]::UTF8.GetString($bytes)
        } catch {
            Write-Host "FAIL: could not decrypt $tokensBlob - wrong Windows user, different machine, or corrupt file?" -ForegroundColor Red
            Write-Host $_.Exception.Message -ForegroundColor Red
            exit 1
        }
    }
    return $plaintext
}

function Write-FtxTokensBlob($tokensBlob, $newPayload) {
    # Assert-NotPackagedShell lives HERE (not just at each caller's top) so
    # every current and future writer of tokens.json.dpapi is covered
    # automatically -- this closes the gap revoke-token.ps1 had: issue-token.ps1
    # guarded itself inline, but revoke-token.ps1 did the identical
    # decrypt-mutate-write-back with no guard at all.
    Assert-NotPackagedShell
    $newBytes  = [System.Text.Encoding]::UTF8.GetBytes($newPayload)
    $newCipher = [System.Security.Cryptography.ProtectedData]::Protect(
        $newBytes, $null, 'CurrentUser')
    $tempBlob  = "$tokensBlob.tmp"
    [System.IO.File]::WriteAllBytes($tempBlob, $newCipher)
    Move-Item -Path $tempBlob -Destination $tokensBlob -Force
}

# --- Own-service recognition --------------------------------------------------
function Test-FtxOwnServiceProcess {
    # True when $ProcId is ftx-mcp's own service: the venv python of THIS repo
    # running "-m service" (the logon task registers exactly that action, so
    # the task's process and a hand-launched one look identical). Used by
    # setup.ps1 step 1 so a re-run over a live service says "stop it" instead
    # of reporting a foreign port conflict.
    #
    # The listener itself OR its direct parent may be the venv "-m service"
    # process (see the launcher note below).
    #
    # Bind + guard like the 9222 check: a dead/unreadable pid yields $false so
    # the caller treats the holder as foreign and still refuses to proceed.
    param(
        [Parameter(Mandatory)][int]$ProcId,
        [Parameter(Mandatory)][string]$RepoRoot
    )
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $lookup = {
        param([int]$LookupId)
        Get-CimInstance Win32_Process -Filter "ProcessId=$LookupId" -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    $isOwn = {
        param($p)
        $c = if ($p) { $p.CommandLine } else { $null }
        if (-not $c) { return $false }
        return [bool](($c -like "*$venvPython*") -and ($c -like "*-m service*"))
    }

    $proc = & $lookup $ProcId
    if (-not $proc) { return $false }
    if (& $isOwn $proc) { return $true }

    # The venv python.exe is a launcher (venv redirector): it spawns the base
    # interpreter as a child, and that child owns the listening socket with a
    # base-python command line. Accept it when its PARENT (one level only, no
    # ancestor walk) is this repo's venv "-m service" process. A missing or
    # unreadable parent yields $false, so the holder is treated as foreign.
    $parentId = $proc.ParentProcessId
    if (-not $parentId) { return $false }
    $parent = & $lookup ([int]$parentId)
    return (& $isOwn $parent)
}

# --- Chrome discovery ---------------------------------------------------------
function Find-FtxChrome {
    # Returns $null on miss; callers decide Fail vs Warn (setup.ps1 needs
    # -NoCdp-aware branching, install-chrome-cdp.ps1 just Warns and returns).
    $chromePaths = @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    )
    return $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
}

# --- Scheduled-task settings --------------------------------------------------
function New-FtxTaskSettings {
    # Restart-on-failure + unlimited ExecutionTimeLimit: the Task Scheduler
    # default (72h) silently kills a long-lived service/chrome mid-week
    # (field-validated fix). Identical block shared by setup.ps1 (main
    # service task) and install-chrome-cdp.ps1 (CDP chrome task).
    New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
}

# --- Service readiness (used by services.ps1 start/status) --------------------
# Each probe is its own function so Pester can mock the process, port and
# /health checks independently (tests/bootstrap/services-status.Tests.ps1).
function Test-FtxServiceProcessAlive {
    <# True while the scheduled task's process is alive (task State = Running). #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([string]$TaskName = $Script:FtxTaskName)
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    return [bool]($t -and $t.State -eq "Running")
}

function Test-FtxPortListening {
    <# True when something listens on the TCP port. Port <= 0 means "no port". #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([int]$Port)
    if ($Port -le 0) { return $false }
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}

function Get-FtxHealth {
    <# One unauthenticated /health probe. Returns an object with State:
       ok (200) | auth (401, service up, auth required) | down (no listener /
       timeout) | err (anything else), plus Detail (version / message). #>
    [CmdletBinding()]
    param([int]$Port = $Script:FtxHttpPort, [int]$TimeoutSec = 5)
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" `
            -TimeoutSec $TimeoutSec -UseBasicParsing
        $version = $null
        try { $version = ($r.Content | ConvertFrom-Json).version } catch { }
        return [PSCustomObject]@{ State = "ok"; Detail = $version }
    } catch {
        # StrictMode-safe: not every exception type has a Response property.
        $respProp = $_.Exception.PSObject.Properties['Response']
        $resp = if ($respProp) { $respProp.Value } else { $null }
        if ($resp -and $resp.StatusCode.value__ -eq 401) {
            return [PSCustomObject]@{ State = "auth"; Detail = $null }
        }
        $first = ($_.Exception.Message -split "`n")[0]
        if ($_.Exception.Message -match "actively refused|Unable to connect|timed out") {
            return [PSCustomObject]@{ State = "down"; Detail = $first }
        }
        return [PSCustomObject]@{ State = "err"; Detail = $first }
    }
}

function Get-FtxServiceReadiness {
    <# Classifies the main service: 'up' (/health answers 200 or 401),
       'starting' (process alive AND every given port listening, but /health
       does not answer yet), or 'down'. #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [string]$TaskName = $Script:FtxTaskName,
        [int[]]$Ports = @($Script:FtxHttpPort, $Script:FtxMcpPort),
        [int]$HealthPort = $Script:FtxHttpPort
    )
    $h = Get-FtxHealth -Port $HealthPort
    if ($h.State -in @("ok", "auth")) { return "up" }
    if ((Test-FtxServiceProcessAlive -TaskName $TaskName) -and
        -not (@($Ports | Where-Object { -not (Test-FtxPortListening -Port $_) }).Count)) {
        return "starting"
    }
    return "down"
}

function Wait-FtxHealth {
    <# Polls /health every IntervalSec until it answers (200 or 401) or TimeoutSec
       of WALL-CLOCK time has elapsed (Stopwatch, not a count of sleeps -- a
       probe can itself take seconds, so counting sleeps overran badly).
       Returns $true as soon as it answers, $false on timeout. Each probe's own
       timeout is capped by the time remaining, so the total overrun is under
       ~1 s. #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [int]$Port = $Script:FtxHttpPort,
        [double]$TimeoutSec = 30,
        [double]$IntervalSec = 1
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $remaining = $TimeoutSec - $sw.Elapsed.TotalSeconds
        $probeTimeout = [int][Math]::Max(1, [Math]::Min(5, [Math]::Ceiling($remaining)))
        $h = Get-FtxHealth -Port $Port -TimeoutSec $probeTimeout
        if ($h.State -in @("ok", "auth")) { return $true }
        $remaining = $TimeoutSec - $sw.Elapsed.TotalSeconds
        if ($remaining -le 0) { return $false }
        $sleepMs = [int][Math]::Ceiling([Math]::Min($IntervalSec, $remaining) * 1000)
        Start-Sleep -Milliseconds $sleepMs
    }
}
