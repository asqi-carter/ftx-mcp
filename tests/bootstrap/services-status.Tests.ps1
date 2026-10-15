# Pester tests for services.ps1 `status` / `start` readiness reporting (ftx-mcp-wvk.5).
# Requires Pester 5+ (stock Windows ships Pester 3.4, which is too old). Install with:
#   Install-Module Pester -MinimumVersion 5.0.0 -Scope CurrentUser -SkipPublisherCheck
# Run: Invoke-Pester tests\bootstrap\services-status.Tests.ps1
# The scheduled task (process), TCP listeners and /health are all mocked;
# no real service is touched and no real time is spent waiting.

BeforeAll {
    . (Join-Path $PSScriptRoot "..\..\bootstrap\_common.ps1")
    $script:services = Join-Path $PSScriptRoot "..\..\bootstrap\services.ps1"

    # Cmdlet-level fakes used by the end-to-end script runs below. services.ps1
    # dot-sources _common.ps1 itself, so the cmdlets (not our helper functions)
    # are what must be mocked there.
    function script:Set-FakeService {
        param([bool]$Alive, [bool]$HttpListening, [bool]$McpListening, [string]$Health)
        $script:fake = @{
            Alive = $Alive; Http = $HttpListening; Mcp = $McpListening; Health = $Health
        }
    }
}

Describe "Get-FtxServiceReadiness" {
    It "status_reports_starting_when_process_alive_ports_listening_health_down" {
        Mock Get-FtxHealth { [PSCustomObject]@{ State = "down"; Detail = "refused" } }
        Mock Test-FtxServiceProcessAlive { $true }
        Mock Test-FtxPortListening { $true }
        Get-FtxServiceReadiness | Should -Be "starting"
    }

    It "status_reports_up_when_health_answers_200" {
        Mock Get-FtxHealth { [PSCustomObject]@{ State = "ok"; Detail = "1.0.9" } }
        Mock Test-FtxServiceProcessAlive { $true }
        Mock Test-FtxPortListening { $true }
        Get-FtxServiceReadiness | Should -Be "up"
    }

    It "status_reports_up_when_health_answers_401" {
        Mock Get-FtxHealth { [PSCustomObject]@{ State = "auth"; Detail = $null } }
        Mock Test-FtxServiceProcessAlive { $true }
        Mock Test-FtxPortListening { $true }
        Get-FtxServiceReadiness | Should -Be "up"
    }

    It "status_reports_down_when_process_not_alive" {
        Mock Get-FtxHealth { [PSCustomObject]@{ State = "down"; Detail = "refused" } }
        Mock Test-FtxServiceProcessAlive { $false }
        Mock Test-FtxPortListening { $true }
        Get-FtxServiceReadiness | Should -Be "down"
    }

    It "status_reports_down_when_a_port_is_not_listening" {
        Mock Get-FtxHealth { [PSCustomObject]@{ State = "down"; Detail = "refused" } }
        Mock Test-FtxServiceProcessAlive { $true }
        Mock Test-FtxPortListening { $Port -ne 8766 }
        Get-FtxServiceReadiness | Should -Be "down"
    }
}

Describe "Wait-FtxHealth" {
    It "start_returns_true_as_soon_as_health_answers" {
        $script:calls = 0
        Mock Start-Sleep { }
        Mock Get-FtxHealth {
            $script:calls++
            if ($script:calls -lt 3) { [PSCustomObject]@{ State = "down"; Detail = "x" } }
            else { [PSCustomObject]@{ State = "ok"; Detail = "1.0.9" } }
        }
        Wait-FtxHealth -TimeoutSec 30 | Should -BeTrue
        $script:calls | Should -Be 3
        Should -Invoke Start-Sleep -Times 2 -Exactly
    }

    It "get_ftx_health_defaults_to_a_5_second_timeout" {
        (Get-Command Get-FtxHealth).Parameters['TimeoutSec'].ParameterType | Should -Be ([int])
        Mock Invoke-WebRequest { [PSCustomObject]@{ Content = '{"version":"1.0.9"}' } }
        Get-FtxHealth | Out-Null
        Should -Invoke Invoke-WebRequest -Times 1 -Exactly -ParameterFilter { $TimeoutSec -eq 5 }
    }

    It "wait_ftx_health_wall_clock_bound_holds_when_each_probe_takes_longer_than_the_interval" {
        # Each probe takes 400 ms (real time), the poll interval is 100 ms and
        # the budget is 1 s. Counting sleeps (the old behaviour) would run
        # ~10 probes = ~4 s; a Stopwatch bound stops after ~1 s (+ one probe).
        $script:probes = 0
        Mock Get-FtxHealth {
            $script:probes++
            [System.Threading.Thread]::Sleep(400)
            [PSCustomObject]@{ State = "down"; Detail = "slow" }
        }
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $r = Wait-FtxHealth -TimeoutSec 1 -IntervalSec 0.1
        $sw.Stop()
        $r | Should -BeFalse
        $sw.Elapsed.TotalSeconds | Should -BeGreaterOrEqual 1.0
        $sw.Elapsed.TotalSeconds | Should -BeLessThan 2.0
        $script:probes | Should -BeLessOrEqual 4
    }

    It "wait_ftx_health_returns_false_after_the_wall_clock_budget_with_fast_probes" {
        Mock Get-FtxHealth { [PSCustomObject]@{ State = "down"; Detail = "x" } }
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        Wait-FtxHealth -TimeoutSec 1 -IntervalSec 0.25 | Should -BeFalse
        $sw.Stop()
        $sw.Elapsed.TotalSeconds | Should -BeLessThan 2.0
    }
}

Describe "services.ps1 end to end (cmdlets mocked)" {
    BeforeEach {
        $global:LASTEXITCODE = $null
        Mock Start-Sleep { }
        Mock Start-ScheduledTask { }
        Mock Stop-ScheduledTask { }
        Mock Get-ScheduledTask {
            if ($TaskName -ne "ftx-mcp") { return $null }
            [PSCustomObject]@{
                State   = $(if ($script:fake.Alive) { "Running" } else { "Ready" })
                Actions = @([PSCustomObject]@{
                    Execute = "python.exe"; Arguments = "-m service"; WorkingDirectory = "C:\x" })
            }
        }
        Mock Get-ScheduledTaskInfo {
            [PSCustomObject]@{ LastRunTime = (Get-Date); LastTaskResult = 0 }
        }
        Mock Get-NetTCPConnection {
            $listening = switch ($LocalPort) {
                8765 { $script:fake.Http }
                8766 { $script:fake.Mcp }
                default { $false }
            }
            if ($listening) { [PSCustomObject]@{ LocalPort = $LocalPort; OwningProcess = 1 } }
        }
        Mock Invoke-WebRequest {
            switch ($script:fake.Health) {
                "ok"   { [PSCustomObject]@{ Content = '{"version":"1.0.9"}' } }
                default { throw [System.Net.WebException]::new("Unable to connect to the remote server") }
            }
        }
    }

    It "status_prints_starting_when_process_alive_ports_listening_health_not_answering" {
        Set-FakeService -Alive $true -HttpListening $true -McpListening $true -Health "none"
        $out = & $script:services status *>&1 | Out-String
        $out | Should -Match "starting"
        $out | Should -Not -Match "/health down"
        # status only calls `exit 1` on a FAULT; here it must not have.
        $LASTEXITCODE | Should -Not -Be 1
    }

    It "status_prints_health_down_when_process_is_not_running" {
        Set-FakeService -Alive $false -HttpListening $false -McpListening $false -Health "none"
        $out = & $script:services status *>&1 | Out-String
        $out | Should -Match "/health down"
        $out | Should -Not -Match "starting"
    }

    It "status_prints_health_200_when_service_answers" {
        Set-FakeService -Alive $true -HttpListening $true -McpListening $true -Health "ok"
        $out = & $script:services status *>&1 | Out-String
        $out | Should -Match "/health 200"
        $out | Should -Not -Match "starting"
    }

    It "status_returns_up_as_soon_as_health_answers" {
        Set-FakeService -Alive $true -HttpListening $true -McpListening $true -Health "ok"
        $out = & $script:services status *>&1 | Out-String
        $out | Should -Match "\bup\b\s+/health 200"
        $out | Should -Not -Match "starting"
        # One /health probe, no polling/sleeping.
        Should -Invoke Invoke-WebRequest -Times 1 -Exactly
        Should -Invoke Start-Sleep -Times 0 -Exactly
    }

    It "start_prints_elapsed_time_with_up_when_health_answers" {
        Set-FakeService -Alive $true -HttpListening $true -McpListening $true -Health "ok"
        $out = & $script:services start *>&1 | Out-String
        $LASTEXITCODE | Should -Be 0
        $out | Should -Match "\bup\b.*after \d+[.,]\d s"
        $out | Should -Not -Match "still starting"
    }

    It "start_prints_still_starting_and_exits_0_when_health_never_answers" {
        Set-FakeService -Alive $true -HttpListening $true -McpListening $true -Health "none"
        # Wait-FtxHealth is wall-clock bounded now, so the mocked sleep must
        # take (a little) real time or the loop would spin through the budget.
        Mock Start-Sleep { [System.Threading.Thread]::Sleep(50) }
        $out = & $script:services start -HealthWaitSec 1 *>&1 | Out-String
        $LASTEXITCODE | Should -Be 0
        $out | Should -Match "still starting"
    }
}
