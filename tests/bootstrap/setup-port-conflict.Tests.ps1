# Pester tests for setup.ps1 step 1 own-service recognition.
# Requires Pester 5+ (stock Windows ships Pester 3.4, which is too old). Install with:
#   Install-Module Pester -MinimumVersion 5.0.0 -Scope CurrentUser -SkipPublisherCheck
# Run: Invoke-Pester tests\bootstrap\setup-port-conflict.Tests.ps1
# The process lookup (Get-CimInstance) is mocked; no real service is touched.

BeforeAll {
    . (Join-Path $PSScriptRoot "..\..\bootstrap\_common.ps1")
    $script:repo = "C:\src\ftx-mcp"
}

Describe "Test-FtxOwnServiceProcess" {
    It "own_service_listener_is_recognised" {
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"C:\src\ftx-mcp\.venv\Scripts\python.exe" -m service' }
        }
        Test-FtxOwnServiceProcess -ProcId 4242 -RepoRoot $script:repo | Should -BeTrue
    }

    It "own_service_with_hide_console_is_recognised" {
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"C:\src\ftx-mcp\.venv\Scripts\python.exe" -m service --hide-console' }
        }
        Test-FtxOwnServiceProcess -ProcId 4242 -RepoRoot $script:repo | Should -BeTrue
    }

    It "foreign_holder_is_not_own_service" {
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"C:\Python312\python.exe" -m http.server 8765' }
        }
        Test-FtxOwnServiceProcess -ProcId 4242 -RepoRoot $script:repo | Should -BeFalse
    }

    It "other_checkout_service_is_not_own_service" {
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"D:\other\.venv\Scripts\python.exe" -m service' }
        }
        Test-FtxOwnServiceProcess -ProcId 4242 -RepoRoot $script:repo | Should -BeFalse
    }

    It "dead_pid_is_treated_as_foreign" {
        Mock Get-CimInstance { $null }
        Test-FtxOwnServiceProcess -ProcId 4242 -RepoRoot $script:repo | Should -BeFalse
    }

    # venv launcher: the listener is a base-python child whose parent is the
    # venv "-m service" process (one level only).
    It "venv_launcher_child_is_recognised_via_parent" {
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"C:\Python312\python.exe" -m service'; ParentProcessId = 100 }
        } -ParameterFilter { $Filter -eq "ProcessId=4242" }
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"C:\src\ftx-mcp\.venv\Scripts\python.exe" -m service'; ParentProcessId = 1 }
        } -ParameterFilter { $Filter -eq "ProcessId=100" }
        Test-FtxOwnServiceProcess -ProcId 4242 -RepoRoot $script:repo | Should -BeTrue
    }

    It "child_of_unrelated_parent_is_not_own_service" {
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"C:\Python312\python.exe" -m service'; ParentProcessId = 200 }
        } -ParameterFilter { $Filter -eq "ProcessId=4242" }
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = 'C:\Windows\explorer.exe'; ParentProcessId = 1 }
        } -ParameterFilter { $Filter -eq "ProcessId=200" }
        Test-FtxOwnServiceProcess -ProcId 4242 -RepoRoot $script:repo | Should -BeFalse
    }

    It "grandparent_is_not_consulted" {
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"C:\Python312\python.exe" -m service'; ParentProcessId = 200 }
        } -ParameterFilter { $Filter -eq "ProcessId=4242" }
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = 'C:\Windows\System32\cmd.exe'; ParentProcessId = 300 }
        } -ParameterFilter { $Filter -eq "ProcessId=200" }
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"C:\src\ftx-mcp\.venv\Scripts\python.exe" -m service'; ParentProcessId = 1 }
        } -ParameterFilter { $Filter -eq "ProcessId=300" }
        Test-FtxOwnServiceProcess -ProcId 4242 -RepoRoot $script:repo | Should -BeFalse
    }

    It "unreadable_parent_is_treated_as_foreign" {
        Mock Get-CimInstance {
            [PSCustomObject]@{ CommandLine = '"C:\Python312\python.exe" -m service'; ParentProcessId = 200 }
        } -ParameterFilter { $Filter -eq "ProcessId=4242" }
        Mock Get-CimInstance { $null } -ParameterFilter { $Filter -eq "ProcessId=200" }
        Test-FtxOwnServiceProcess -ProcId 4242 -RepoRoot $script:repo | Should -BeFalse
    }
}
