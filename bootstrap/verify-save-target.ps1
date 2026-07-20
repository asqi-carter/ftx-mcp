<#
.SYNOPSIS
    Verify that optix_save's Ctrl+S targets the SAME Studio instance whose
    project the design-time bridge is serving.

.DESCRIPTION
    The design-time bridge (a NetLogic) runs INSIDE the Studio process, so the
    PID that owns TCP :8768 IS the Studio instance holding the bridge-served
    project. save() (service/core.py _build_save_ps) targets the *first*
    `FTOptixStudio` process with a non-empty MainWindowTitle+MainWindowHandle
    (`Select-Object -First 1`). With one Studio instance those coincide; with
    two open projects the "-First 1" pick can land Ctrl+S on the WRONG project.

    This verifier mirrors save()'s exact selection and asserts it equals the
    bridge's owning PID. Read-only - sends no keystroke, changes nothing.
    Exit 0 = PASS, 1 = MISMATCH (save could hit the wrong project), 2 = can't
    evaluate (bridge down / no Studio window).
#>
[CmdletBinding()]
param([int]$Port = 8768)
$ErrorActionPreference = 'Stop'

# 1. What project is the bridge serving?
try {
    $bh = (Invoke-WebRequest "http://127.0.0.1:$Port/bridge/health" -UseBasicParsing -TimeoutSec 4).Content | ConvertFrom-Json
} catch {
    Write-Output "SKIP: bridge not reachable on :$Port (open the project in Studio and run StartBridge)"; exit 2
}
Write-Output ("bridge:        project=$($bh.project) model_loaded=$($bh.model_loaded) version=$($bh.bridge_version)")

# 2. PID that owns the :8768 listener == the Studio instance running the bridge
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) { Write-Output "SKIP: nothing listening on :$Port"; exit 2 }
$bridgePid  = [int]$conn.OwningProcess
$bridgeProc = Get-Process -Id $bridgePid -ErrorAction SilentlyContinue
$bridgeIsStudio = $bridgeProc -and ($bridgeProc.ProcessName -ieq 'FTOptixStudio')
Write-Output ("bridge owner:  pid=$bridgePid name=$($bridgeProc.ProcessName) isStudio=$bridgeIsStudio")

# 3. The instance save() would target - SAME selection as _build_save_ps
$studios = @(Get-Process FTOptixStudio -ErrorAction SilentlyContinue)
$target  = $studios | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -ne '' } | Select-Object -First 1
Write-Output ("studio procs:  total=$($studios.Count) with-window=$(@($studios | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -ne '' }).Count)")
if (-not $target) { Write-Output "SKIP: no focus-able Studio window for save() to target"; exit 2 }
$savePid = [int]$target.Id
Write-Output ("save target:   pid=$savePid title='$($target.MainWindowTitle)'")

# 4. Verdict
if (-not $bridgeIsStudio) {
    Write-Output "WARN: :$Port owner is not FTOptixStudio - bridge in an unexpected host; cannot vouch for the save target"; exit 1
}
if ($savePid -eq $bridgePid) {
    Write-Output "PASS: Ctrl+S targets the bridge's Studio instance (save pid == bridge pid == $bridgePid)"; exit 0
}
Write-Output "FAIL: MISMATCH - save() would focus pid=$savePid but the bridge (project '$($bh.project)') runs in pid=$bridgePid. Ctrl+S could save the WRONG project. Close the extra Studio instance so the save targets the right project."
exit 1
