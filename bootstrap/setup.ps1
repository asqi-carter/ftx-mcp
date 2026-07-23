<#
.SYNOPSIS
    Idempotent installer for ftx-mcp on Windows 11.

.DESCRIPTION
    Fresh-Windows-box -> working stack. See docs/architecture.md.
    Run from the repo root or from an extracted release tarball.

    Two deploy paths ship: the UpdateSvc deploy verb (the primary ship
    step; needs the deploy env configured -- run optix_doctor) and the
    export-based tree swap (Studio must be closed). Deploy integration is
    not wired in this distribution.

    Steps:
      1. Port-conflict detection (8081, 8765, 8766, 9222 + HMI port)
      2. Verify FT Optix Studio installed
      3. Verify Chrome installed (skipped if -NoCdp)
      3.5 Install Tesseract OCR via winget if missing (-NoOcr skips)
      4. Create state dirs %LOCALAPPDATA%\ftx-mcp\{logs,secrets,export-staging,runtime}
      5. Install Python 3.12 via winget if missing; create venv; install package
      6. Bearer-token auth bootstrap (HMI tokens.json.dpapi)
      7. Register ftx-mcp scheduled task at user logon
      8. Optionally install the Chrome-CDP verify task (-NoCdp to skip)
      9. Verify GET /health returns 200; print summary

.PARAMETER NoCdp
    Skip step 8 entirely. Minimum viable install: ftx-mcp alone.

.PARAMETER NoOcr
    Skip the step 3.5 Tesseract install. The OCR-backed text tools
    (read_text, find_text, navigate expect_text, sweep manifests) then
    report tesseract_not_installed; everything else is unaffected.

.PARAMETER NoAuth
    Loopback-no-auth opt-out. Sets FTX_AUTH_REQUIRED=false at user-env
    scope and skips Step 6 token issuance entirely. The service banner
    will read "auth  disabled (loopback only)" on next start, and the
    LAN-bind refusal matrix will block any non-loopback bind. Use only
    on single-user dev boxes where bearer-token friction outweighs the
    threat model. See docs/security.md.

    NOTE: this writes FTX_AUTH_REQUIRED=false at User env scope, so the
    setting persists across logons and across re-installs. To revert
    -NoAuth later, run:
      [Environment]::SetEnvironmentVariable("FTX_AUTH_REQUIRED",$null,"User")

.PARAMETER EnableAuth
    Opt in to bearer-token auth (the LAN posture): persists
    FTX_AUTH_REQUIRED=true at User scope and issues a bootstrap token.
    Required before OPTIX_BIND_HOST=0.0.0.0 - the service refuses a LAN
    bind without auth. Without this switch, setup leaves auth OFF
    (loopback-only default) and never prompts.

.PARAMETER NoAuthPrompt
    DEPRECATED no-op (accepted for script back-compat). The interactive
    auth prompt was removed in v1.0.3; the default is auth-off and
    enabling is explicit via -EnableAuth.

.PARAMETER NoServiceRegister
    Skip Step 7 (scheduled-task register) and Step 9 (/health probe).
    Useful for install-smoke runs that exercise Steps 0-6 + 8 in a
    redirected state dir + ports (override via OPTIX_STATE_DIR /
    OPTIX_HTTP_PORT / OPTIX_MCP_PORT) without touching the prod
    scheduled task. After install completes the operator (or CI)
    manually launches the service from the freshly-installed venv to
    smoke-probe /health.

.PARAMETER RepoRoot
    Path to the ftx-mcp checkout (default: parent of this script).
#>
[CmdletBinding()]
param(
    [switch]$NoCdp,
    [switch]$NoAuth,
    [switch]$NoOcr,
    [switch]$EnableAuth,
    [switch]$NoAuthPrompt,
    [switch]$NoServiceRegister,
    [string]$RepoRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

# Install-time state dir matches what the service resolves at runtime via
# core.Config.from_env() - both respect OPTIX_STATE_DIR. Override at
# install time for install-smoke runs (alongside -NoServiceRegister) or
# corp-IT boxes that redirect %LOCALAPPDATA%. Default keeps the
# fresh-Windows-box install at %LOCALAPPDATA%\ftx-mcp\.
if ($env:OPTIX_STATE_DIR) {
    $state = $env:OPTIX_STATE_DIR
} else {
    $state = Join-Path $env:LOCALAPPDATA "ftx-mcp"
}
$secretsDir = Join-Path $state "secrets"
$logsDir = Join-Path $state "logs"
$exportStagingDir = Join-Path $state "export-staging"
$runtimeDir = Join-Path $state "runtime"
$venvDir = Join-Path $RepoRoot ".venv"

function Section($name) {
    Write-Host ""
    Write-Host "=== $name ===" -ForegroundColor Cyan
}

function Fail($msg) {
    Write-Host "FAIL: $msg" -ForegroundColor Red
    exit 1
}

function Ok($msg) {
    Write-Host "ok: $msg" -ForegroundColor Green
}

function Warn($msg) {
    Write-Host "WARN: $msg" -ForegroundColor Yellow
}

# Step 0: ExecutionPolicy nudge
# PowerShell will refuse to dot-source helper scripts (services.ps1,
# other bootstrap scripts) under Restricted/AllSigned/Undefined.
# Catch that here with a clear remedy instead of failing opaquely later.
# The check uses the EFFECTIVE policy (not just CurrentUser scope): a box
# whose policy comes from MachinePolicy/LocalMachine would otherwise pass
# or fail on the wrong scope's value.
$execPolicy = Get-ExecutionPolicy
if ($execPolicy -in @('Restricted', 'AllSigned', 'Undefined')) {
    Write-Host ""
    Write-Host "Effective ExecutionPolicy is '$execPolicy'." -ForegroundColor Yellow
    Write-Host "ftx-mcp setup runs .ps1 helpers; this policy will block them." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Run this in the same shell, then re-run setup.ps1:" -ForegroundColor Yellow
    Write-Host "    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "If this repo came from a GitHub ZIP download (not git clone)," -ForegroundColor Yellow
    Write-Host "also clear the mark-of-the-web or RemoteSigned still blocks it:" -ForegroundColor Yellow
    Write-Host "    Get-ChildItem -Recurse | Unblock-File" -ForegroundColor Cyan
    Write-Host ""
    Fail "ExecutionPolicy '$execPolicy' blocks helper scripts. See remedy above."
}
Ok "ExecutionPolicy (effective) = $execPolicy"

# Elevation check: setup needs NO admin rights, and tasks registered from an
# elevated shell get Admins-owned descriptors - uninstall/re-register then
# demands elevation forever after (field report 2026-07-22). Warn, don't block:
# corp boxes sometimes only hand out elevated shells.
$isElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isElevated) {
    Write-Host "note: running ELEVATED. Setup does not need admin - scheduled tasks registered from an elevated shell will require an elevated uninstall later. Prefer a regular PowerShell window." -ForegroundColor Yellow
}

# Surface persisted auth state up front. FTX_AUTH_REQUIRED lives at User env
# scope and survives re-installs BY DESIGN (see Step 6) - which reads as
# "auth mysteriously on" when a re-install follows an earlier auth-on choice.
# Field report 2026-07-22: cost a support round-trip. One line here ends that.
$persistedAuth = [Environment]::GetEnvironmentVariable("FTX_AUTH_REQUIRED", "User")
if ($persistedAuth -eq "true") {
    Write-Host "note: FTX_AUTH_REQUIRED=true persisted from a previous install - the UI/API will demand a bearer token. Re-run with -NoAuth to force off (single-user dev box)." -ForegroundColor Yellow
} elseif ($persistedAuth) {
    Ok "FTX_AUTH_REQUIRED (User) = $persistedAuth (persisted)"
}

# Step 0.5: refuse to run inside an MSIX-packaged shell.
# A shell hosted by a packaged app (e.g. the Microsoft Store build of
# Claude Desktop - exactly what docs/cowork-quick-install.md used to
# produce) runs with filesystem write virtualization: every write this
# script makes under %LOCALAPPDATA% lands in the app's private
# LocalCache overlay. In-shell checks see the merged view and pass, but
# the scheduled tasks registered below run OUTSIDE the package against
# the real filesystem - where none of those writes exist. The service
# self-creates its state dirs at startup (service/main.py), but
# token/secret writes (issue-token.ps1) have no such recovery, so the
# only safe behavior is to refuse and point at a regular shell.
$pkgSig = @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
public static extern int GetCurrentPackageFullName(ref uint length, System.Text.StringBuilder fullName);
'@
$pkgType = Add-Type -MemberDefinition $pkgSig -Name PkgIdentity -Namespace FtxSetup -PassThru
$pkgLen = [uint32]0
# 15700 = APPMODEL_ERROR_NO_PACKAGE -> unpackaged process, safe to proceed
if ($pkgType::GetCurrentPackageFullName([ref]$pkgLen, $null) -ne 15700) {
    Fail ("This shell is running inside an MSIX-packaged app (e.g. the Microsoft " +
          "Store build of Claude Desktop). Its writes to %LOCALAPPDATA% are " +
          "virtualized into the app's private LocalCache and invisible to the " +
          "ftx-mcp scheduled tasks. Re-run setup.ps1 from a regular PowerShell window.")
}

# Step 1: port-conflict detection
Section "1. Port-conflict detection"
# Resolve port set from env overrides so install-smoke runs (with the
# service ports redirected) don't false-positive on the prod service
# holding the default ports. Each port mirrors the service-side
# resolution in core.Config.from_env() / install-chrome-cdp.ps1.
$httpPort        = $env:OPTIX_HTTP_PORT;         if (-not $httpPort)        { $httpPort = 8765 }
$mcpPort         = $env:OPTIX_MCP_PORT;          if (-not $mcpPort)         { $mcpPort = 8766 }
$runtimeTestPort = $env:OPTIX_RUNTIME_TEST_PORT; if (-not $runtimeTestPort) { $runtimeTestPort = 8081 }
# 9222 is the Chrome CDP port owned by install-chrome-cdp.ps1; check
# only when the chrome-cdp task is going to install. -NoCdp skips it.
$ports = @([int]$runtimeTestPort, [int]$httpPort, [int]$mcpPort)
if (-not $NoCdp) { $ports += 9222 }
$conflicts = @()
foreach ($p in $ports) {
    $listener = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        $conflicts += [PSCustomObject]@{
            Port = $p
            Pid  = ($listener | Select-Object -First 1).OwningProcess
        }
    } else {
        Ok "$p free"
    }
}
if ($conflicts.Count -gt 0) {
    Write-Host ""
    Write-Host "Port conflicts detected:" -ForegroundColor Yellow
    $conflicts | Format-Table -AutoSize | Out-String | Write-Host
    # Per-conflict guidance. 9222 gets its own remedy: env vars don't move it,
    # and the most common holder (field report 2026-07-22) is a leftover
    # ftx-mcp CDP chrome from a previous install - identified by its dedicated
    # profile dir, in which case services.ps1 stop reaps it.
    $cdpMarker = Join-Path $env:LOCALAPPDATA "ftx-mcp\chrome-cdp-profile"
    foreach ($c in $conflicts) {
        if ($c.Port -eq 9222) {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($c.Pid)" -ErrorAction SilentlyContinue).CommandLine
            if ($cmd -and $cmd -like "*$cdpMarker*") {
                Write-Host "  :9222 is ftx-mcp's own CDP chrome (pid $($c.Pid), likely a previous install)." -ForegroundColor Yellow
                Write-Host "  Stop it:  .\bootstrap\services.ps1 stop    (then re-run setup)" -ForegroundColor Yellow
            } else {
                Write-Host "  :9222 is held by pid $($c.Pid) (not ftx-mcp's CDP chrome)." -ForegroundColor Yellow
                Write-Host "  Close that app (often a browser started with remote debugging), or re-run with -NoCdp to skip canvas verify." -ForegroundColor Yellow
            }
        } else {
            Write-Host "  :$($c.Port) - override with OPTIX_HTTP_PORT (8765) / OPTIX_MCP_PORT (8766) / OPTIX_RUNTIME_TEST_PORT (8081) and re-run." -ForegroundColor Yellow
        }
    }
    Fail "Resolve port conflicts before continuing."
}

# Step 2: verify FT Optix Studio
# Honors a pre-set FTOPTIX_STUDIO_EXE (Process or User scope) before probing
# the default install root. Corp IT relocations and side-by-side Studio
# versions need this override; without it the highest-version probe under
# the default root wins, which is wrong when the operator has explicitly
# pinned a Studio binary via env.
Section "2. Verify FT Optix Studio"
if ($env:FTOPTIX_STUDIO_EXE) {
    if (-not (Test-Path $env:FTOPTIX_STUDIO_EXE)) {
        Fail "FTOPTIX_STUDIO_EXE points at $($env:FTOPTIX_STUDIO_EXE) but the file is missing. Unset it or correct the path."
    }
    $studioExe = Get-Item $env:FTOPTIX_STUDIO_EXE
    Ok "Studio $($studioExe.VersionInfo.FileVersion) at $($studioExe.FullName) (from FTOPTIX_STUDIO_EXE)"
} else {
    $studioRoot = "C:\Program Files\Rockwell Automation\FactoryTalk Optix"
    if (-not (Test-Path $studioRoot)) {
        Fail "FT Optix Studio not found under $studioRoot. Install Studio first, or set FTOPTIX_STUDIO_EXE to its full path."
    }
    $studioExe = Get-ChildItem -Path $studioRoot -Recurse -Filter FTOptixStudio.exe -ErrorAction SilentlyContinue |
        Sort-Object @{Expression = { $_.VersionInfo.FileVersion }; Descending = $true } |
        Select-Object -First 1
    if (-not $studioExe) {
        Fail "FTOptixStudio.exe not found under $studioRoot. Set FTOPTIX_STUDIO_EXE to its full path to override."
    }
    Ok "Studio $($studioExe.VersionInfo.FileVersion) at $($studioExe.FullName)"
    $env:FTOPTIX_STUDIO_EXE = $studioExe.FullName
}

# Step 3: verify Chrome
Section "3. Verify Chrome"
$chromePaths = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) {
    if ($NoCdp) {
        Write-Host "WARN: Chrome not found, but -NoCdp is set; continuing." -ForegroundColor Yellow
    } else {
        Fail "Chrome not found. Install Chrome or re-run with -NoCdp."
    }
} else {
    Ok "Chrome at $chrome"
}

# Step 3.5: Tesseract OCR (the zero-vision-token text tools: read_text,
# find_text, navigate expect_text, sweep manifests). Optional - everything
# else works without it - so install failures WARN and continue. The
# UB-Mannheim/winget installer does not touch PATH; the service resolves the
# standard install dirs itself, so no PATH edit is needed here either.
Section "3.5 Tesseract OCR (text tools; -NoOcr skips)"
$tessPaths = @(
    "C:\Program Files\Tesseract-OCR\tesseract.exe",
    "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    (Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR\tesseract.exe")
)
$tess = Get-Command tesseract -ErrorAction SilentlyContinue
if ($tess) { $tess = $tess.Source } else { $tess = $tessPaths | Where-Object { Test-Path $_ } | Select-Object -First 1 }
if ($tess) {
    Ok "Tesseract at $tess"
} elseif ($NoOcr) {
    Ok "Tesseract not found; -NoOcr set - text tools will report tesseract_not_installed"
} else {
    Write-Host "Tesseract not found - installing via winget..." -ForegroundColor Yellow
    winget install --id UB-Mannheim.TesseractOCR --silent --accept-source-agreements --accept-package-agreements
    $tess = $tessPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($tess) {
        Ok "Tesseract installed at $tess"
    } else {
        Warn "Tesseract install did not complete (winget missing or blocked). Text tools degrade gracefully; install later with: winget install UB-Mannheim.TesseractOCR"
    }
}

# Step 4: state dirs
Section "4. State dirs"
foreach ($d in @($state, $logsDir, $secretsDir, $exportStagingDir, $runtimeDir)) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Ok "created $d"
    } else {
        Ok "$d"
    }
}

# Step 5: Python + venv + package install
Section "5. Python venv"
# Fresh Win11 ships a Microsoft Store alias stub at ...\WindowsApps\python.exe
# that opens the Store instead of running Python, so Get-Command alone reports
# Python "present" on a box that has none. Treat the stub as absent so the
# winget branch actually fires (the Store alias otherwise shadows real installs).
function Get-RealPython {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*\WindowsApps\python.exe") { return $cmd }
    return $null
}
$python = Get-RealPython
if (-not $python) {
    Write-Host "Python not on PATH (or Store-alias stub only). Installing via winget..." -ForegroundColor Yellow
    winget install --silent --accept-source-agreements --accept-package-agreements Python.Python.3.12
    # winget writes the new PATH to the registry; this session's PATH predates
    # the install. Refresh from registry or the re-check below can't see it.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $python = Get-RealPython
    if (-not $python) { Fail "winget install python failed; install manually." }
}
Ok "Python at $($python.Source)"

if (-not (Test-Path $venvDir)) {
    & $python.Source -m venv $venvDir
    Ok "venv created at $venvDir"
} else {
    Ok "venv exists at $venvDir"
}

$venvPython = Join-Path $venvDir "Scripts\python.exe"
& $venvPython -m pip install --quiet --upgrade pip
# [visual] extra = Pillow, the pixel gate for optix_cdp_diff. Cheap pure
# wheel; installed by default in the full local setup (the lean base matters
# for pip consumers of the package, not for this repo install).
& $venvPython -m pip install --quiet -e "$RepoRoot[visual]"
Ok "ftx-mcp installed into venv"

# Step 6: bearer-token auth bootstrap
# Default: auth OFF (loopback-only install; a bearer token adds ~no security
# on your own box). Opt IN via -EnableAuth - required only for a LAN bind,
# which the service otherwise refuses. -NoAuth forces off + persists the
# choice. No interactive prompt (removed v1.0.3 - it scared fresh installers
# into enabling auth by accident).
Section "6. Bearer-token auth (default: off, loopback-only)"
$tokensBlob = Join-Path $secretsDir "tokens.json.dpapi"
# Pre-clean any stale OPTIX_AUTH_REQUIRED user env var from an old install.
# The env-var rename made it dead; leaving it set would confuse operators
# inspecting their env.
$staleOptixEnv = [Environment]::GetEnvironmentVariable("OPTIX_AUTH_REQUIRED", "User")
if ($staleOptixEnv) {
    [Environment]::SetEnvironmentVariable("OPTIX_AUTH_REQUIRED", $null, "User")
    Write-Host "WARN: cleared stale OPTIX_AUTH_REQUIRED user env (renamed to FTX_AUTH_REQUIRED)." -ForegroundColor Yellow
}
$existingAuthEnv = [Environment]::GetEnvironmentVariable("FTX_AUTH_REQUIRED", "User")
$tokensExist = Test-Path $tokensBlob

if ($NoAuth) {
    # Explicit force-off (same as the v1.0 default; kept for scripts/back-compat).
    [Environment]::SetEnvironmentVariable("FTX_AUTH_REQUIRED", "false", "User")
    $env:FTX_AUTH_REQUIRED = "false"
    Ok "auth disabled (loopback-only) [-NoAuth]"
} elseif ($existingAuthEnv -eq "true") {
    # Operator previously chose auth-on - honour it and ensure a token exists.
    Ok "FTX_AUTH_REQUIRED=true (auth on, operator-set)"
    if (-not $tokensExist) {
        & (Join-Path $PSScriptRoot "issue-token.ps1") -Label "bootstrap" -Scope "deploy" -RepoRoot $RepoRoot
        if ($LASTEXITCODE -ne 0) { Fail "issue-token.ps1 failed; resolve before continuing." }
    } else {
        Ok "tokens.json.dpapi already present at $tokensBlob"
    }
} elseif ($EnableAuth) {
    # Explicit LAN-posture opt-in (replaces the removed interactive prompt).
    [Environment]::SetEnvironmentVariable("FTX_AUTH_REQUIRED", "true", "User")
    $env:FTX_AUTH_REQUIRED = "true"
    Ok "set user env FTX_AUTH_REQUIRED=true [-EnableAuth]"
    & (Join-Path $PSScriptRoot "issue-token.ps1") -Label "bootstrap" -Scope "deploy" -RepoRoot $RepoRoot
    if ($LASTEXITCODE -ne 0) { Fail "issue-token.ps1 failed; resolve before continuing." }
} else {
    # No interactive prompt: field-observed that a y/N here scares fresh
    # installers into answering y, then locking themselves out of the UI.
    # Default is off (loopback-only; a bearer token adds ~no security when
    # every local process already runs as you). LAN users opt in explicitly.
    if ($NoAuthPrompt) {
        Write-Host "note: -NoAuthPrompt is deprecated (the prompt no longer exists); default applies." -ForegroundColor DarkGray
    }
    Ok "auth off (loopback-only default). LAN bind needs auth: re-run with -EnableAuth (see docs/security.md)."
}

# Step 7: register ftx-mcp scheduled task
# Skip with -NoServiceRegister for CI / install-smoke runs that exercise
# steps 0-6 + 8 in a redirected state dir without touching the prod
# scheduled task. The flag also short-circuits step 10 (the /health
# probe expects a running service).
Section "7. Register ftx-mcp scheduled task"
if ($NoServiceRegister) {
    Ok "skipped (-NoServiceRegister)"
    Write-Host "  Service task not registered; no auto-start at logon." -ForegroundColor DarkGray
    Write-Host "  To launch manually from this checkout:" -ForegroundColor DarkGray
    Write-Host "    & '$venvPython' -m service" -ForegroundColor DarkGray
    Write-Host "  (respects OPTIX_STATE_DIR / OPTIX_HTTP_PORT / OPTIX_MCP_PORT env)" -ForegroundColor DarkGray
} else {
    $taskName = "ftx-mcp"
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }

    $action = New-ScheduledTaskAction `
        -Execute $venvPython `
        -Argument "-m service" `
        -WorkingDirectory $RepoRoot
    # No -Trigger: the task is manual-only by design; start via
    # bootstrap/services.ps1 start (or Start-ScheduledTask). This keeps a
    # fresh logon quiet for developers who aren't actively touching Optix.
    # ExecutionTimeLimit 0 = unlimited: the Task Scheduler default (72h)
    # silently kills a long-lived service mid-week (field-validated fix).
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Settings $settings `
        -Principal $principal | Out-Null
    Ok "scheduled task '$taskName' registered (manual start; use bootstrap\services.ps1)"

    # Optional: kick it off now so the smoke test below can hit /health
    Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

# Step 8: Chrome CDP (optional, for canvas verify)
# The chrome-cdp task lets the service screenshot + click the rendered HMI
# canvas via CDP. Authoring works without it; skip on locked-down boxes
# that block Chrome or CDP port 9222.
Section "8. Chrome CDP (verify)"
if ($NoCdp) {
    Ok "skipped (-NoCdp)"
    Write-Host "  To install the CDP-Chrome verify task later:" -ForegroundColor DarkGray
    Write-Host "    powershell -File bootstrap\install-chrome-cdp.ps1 -RepoRoot $RepoRoot" -ForegroundColor DarkGray
    Write-Host "  Canvas verify (optix_cdp_screenshot/click) is disabled until then;" -ForegroundColor DarkGray
    Write-Host "  deploy / runtime_start still work via HTTP + MCP." -ForegroundColor DarkGray
} else {
    & (Join-Path $PSScriptRoot "install-chrome-cdp.ps1") -RepoRoot $RepoRoot
}

# Step 10: verify /health
# Probes the loopback HTTP port for a 200 response. Skipped when
# -NoServiceRegister is set (no service was registered/started; the
# probe would always time out).
Section "9. Verify /health"
if ($NoServiceRegister) {
    Ok "skipped (-NoServiceRegister; no service was registered to probe)"
} else {
    $healthPort = $env:OPTIX_HTTP_PORT
    if (-not $healthPort) { $healthPort = 8765 }
    $ok = $false
    for ($i = 0; $i -lt 10; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$healthPort/health" -TimeoutSec 2 -UseBasicParsing
            if ($resp.StatusCode -eq 200) { $ok = $true; break }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    if (-not $ok) {
        Write-Host "WARN: /health did not return 200 within 10s. Check the scheduled task logs at $logsDir." -ForegroundColor Yellow
    } else {
        Ok "/health returned 200"
        $health = $resp.Content | ConvertFrom-Json
        $health | Format-List | Out-String | Write-Host
        # HTTP 200 alone is not a healthy install: an MSIX-virtualized setup
        # shell produced a service that answered 200 while runtime_dir_exists
        # was false (the dirs existed only in the package overlay). Assert
        # the *_exists flags so a split-brain install fails HERE, at install
        # time, with the flag named. projects_root_exists stays a WARN -
        # Studio only creates that directory the first time a project is
        # saved, so it is legitimately absent on a fresh box.
        $badFlags = $health.PSObject.Properties |
            Where-Object { $_.Name -like '*_exists' -and $_.Value -eq $false } |
            Select-Object -ExpandProperty Name
        $fatalFlags = @($badFlags | Where-Object { $_ -ne 'projects_root_exists' })
        if ($badFlags -contains 'projects_root_exists') {
            Warn "projects_root does not exist yet (Studio creates it on first project save)."
        }
        if ($fatalFlags.Count -gt 0) {
            Fail ("/health returned 200 but reported: " + ($fatalFlags -join ', ') +
                  " = false. The service sees a different filesystem state than " +
                  "this shell - if setup ran inside a packaged app shell, re-run " +
                  "from a regular PowerShell window.")
        }
        Ok "/health *_exists flags verified"
    }
}

Section "Done"
Write-Host "ftx-mcp install complete." -ForegroundColor Green
Write-Host "  HTTP   http://127.0.0.1:$httpPort"
Write-Host "  MCP    http://127.0.0.1:$mcpPort/mcp"
Write-Host "  state  $state"
Write-Host "  repo   $RepoRoot"
Write-Host ""
$authOn = ([Environment]::GetEnvironmentVariable("FTX_AUTH_REQUIRED", "User")) -eq "true"
if ($authOn) {
    Write-Host "Next: add the MCP server to your client config:" -ForegroundColor Cyan
    Write-Host "  http://127.0.0.1:$mcpPort/mcp  (auth is enabled; set an Authorization: Bearer <token> header)" -ForegroundColor Cyan
} else {
    Write-Host "Next: register with Claude Code (no bearer; loopback-no-auth):" -ForegroundColor Cyan
    Write-Host "  claude mcp add --transport http ftx-mcp http://127.0.0.1:$mcpPort/mcp" -ForegroundColor Cyan
}
Write-Host ""
Write-Host "Verify your setup: ask your AI assistant to run the 'optix_doctor' tool" -ForegroundColor Cyan
Write-Host "  (or GET /doctor): it checks every dependency (Studio, projects root," -ForegroundColor Cyan
Write-Host "  cdp, deploy account/cert, interactive session) and prints a plain-" -ForegroundColor Cyan
Write-Host "  English fix for anything red. Run it first if a later step doesn't work." -ForegroundColor Cyan
Write-Host ""
Write-Host "Lifecycle: .\bootstrap\services.ps1 {start|stop|restart|status}" -ForegroundColor Cyan
