<#
.SYNOPSIS
    Register the chrome-cdp at-logon task - the Chrome the service drives via
    CDP for reliable Optix-canvas screenshots + clicks.

.DESCRIPTION
    Launches Chrome headless with --remote-debugging-port=9222 (manual start,
    via bootstrap/services.ps1 start; -Headed for a visible window). The
    service connects over CDP to take
    trusted screenshots and dispatch trusted mouse events on the runtime
    canvas - see service/_cdp.py.

    Window-size pinning: Chrome opens at 800x600 to match the default
    Optix MainWindow so canvas coordinates are deterministic. If your
    MainWindow differs, edit the --window-size arg. See
    docs/optix-patterns/canvas-coordinate-reference.md.

    Cert tolerance: --ignore-certificate-errors so CDP verify works against an
    Optix Web presentation engine serving HTTPS with its self-signed runtime
    cert. A fresh project's Web engine defaults to https:443; without this
    flag Chrome shows the
    NET::ERR_CERT_AUTHORITY_INVALID interstitial instead of the canvas and
    every screenshot/click captures the warning page. Scope is this dedicated
    CDP profile only (loopback runtime), not the user's browsing.
#>
param(
    [string]$RepoRoot,
    # Default is headless (--headless=new): no window to accidentally close, no
    # Chrome popping up during automation, and it renders the Optix canvas via
    # SwiftShader identically to headed. Pass -Headed
    # to run a visible window for debugging.
    [switch]$Headed
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Ok($msg) { Write-Host "ok: $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }

$chromePaths = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { Warn "Chrome not found; skipping CDP launcher task"; return }

$chromeProfile = Join-Path $env:LOCALAPPDATA "ftx-mcp\chrome-cdp-profile"
New-Item -ItemType Directory -Path $chromeProfile -Force | Out-Null
# Headless by default (SwiftShader so the WebGL/canvas still renders with no
# GPU). --window-size still pins canvas coords in headless. -Headed opts out.
$headlessArgs = if ($Headed) { "" } else { " --headless=new --use-angle=swiftshader --enable-unsafe-swiftshader" }
$chromeArgs = "--remote-debugging-port=9222 --user-data-dir=`"$chromeProfile`" --no-first-run --no-default-browser-check --ignore-certificate-errors --window-size=800,600$headlessArgs"

$existing = Get-ScheduledTask -TaskName "ftx-mcp-chrome-cdp" -ErrorAction SilentlyContinue
if ($existing) { Unregister-ScheduledTask -TaskName "ftx-mcp-chrome-cdp" -Confirm:$false }

$action = New-ScheduledTaskAction -Execute $chrome -Argument $chromeArgs
# No -Trigger. Start via bootstrap/services.ps1 start.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
# Restart-on-failure + unlimited ExecutionTimeLimit: the scheduler's 72h
# default otherwise kills the headless chrome mid-week, and a crashed
# chrome silently strands canvas verify (field-validated fix). Restarts
# only cover mid-session crashes - after logoff, services.ps1 start.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask `
    -TaskName "ftx-mcp-chrome-cdp" `
    -Action $action -Principal $principal -Settings $settings | Out-Null
$mode = if ($Headed) { "headed" } else { "headless" }
Ok "Chrome CDP scheduled task registered ($mode; manual start; --remote-debugging-port=9222)"
