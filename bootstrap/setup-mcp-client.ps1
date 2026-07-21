<#
.SYNOPSIS
    One-shot: wire ftx-mcp into a Claude Desktop (or Cowork) client
    config. Issues and embeds a bearer token only when auth is enabled.

.DESCRIPTION
    Writes/merges the `ftx-mcp` server block into Claude Desktop's
    claude_desktop_config.json. Local MCP servers in that file are also
    auto-bridged into Claude Cowork (they show up under Settings -> Connectors
    with a "Local dev" badge - that listing is informational; local servers are
    configured HERE, never via Connectors -> Add).

    Auth is OFF by default on the loopback install, so by default NO token is
    issued or embedded - the config is just the server URL. When
    FTX_AUTH_REQUIRED=true (or -WithToken / -Bearer is passed), a scoped token
    is issued via issue-token.ps1 -Json and embedded with no manual copy step.

    Default emits the stdio mcp-remote form wrapped in `cmd /c` (needs Node/npx):
        "ftx-mcp": { "command": "cmd", "args": ["/c","npx","-y","mcp-remote",
                     "http://127.0.0.1:8766/mcp","--header","Authorization: Bearer <token>"] }
    This is what the Microsoft Store (MSIX) Claude Desktop build loads. -NativeHttp
    emits the dependency-free `type: http` form for builds that support it.

    LOCAL MODEL: the service, Studio, and Claude Desktop all run on THIS machine.
    The service binds 127.0.0.1 and the emitted config points at 127.0.0.1 -- keep
    it that way. Nothing is exposed on the network; do not bind 0.0.0.0.

.PARAMETER Label      Token label (default 'claude-desktop').
.PARAMETER Scope      health | read | deploy (default 'deploy' - full loop).
.PARAMETER Bearer     Reuse an existing bearer instead of issuing a new one.
.PARAMETER WithToken  Force issuing/embedding a token even when auth is off.
.PARAMETER BindHost   Host in the emitted URL (default 127.0.0.1).
.PARAMETER Port       MCP port (default 8766).
.PARAMETER NativeHttp  Emit the type:http form instead of the default cmd/c mcp-remote.
.PARAMETER WriteConfig   Write/merge into claude_desktop_config.json. Without it,
                         the block is printed for you to paste.
.PARAMETER ServerName  Key under mcpServers (default 'ftx-mcp').

.EXAMPLE
    .\bootstrap\setup-mcp-client.ps1 -WriteConfig
    Writes the ftx-mcp block into Claude Desktop's config (no token on a
    default install). Restart Claude Desktop and the tools appear.

.EXAMPLE
    .\bootstrap\setup-mcp-client.ps1 -Scope read -NativeHttp -WithToken
    Prints a type:http config block with a read-only token, to paste manually.
#>
[CmdletBinding()]
param(
    [string]$Label = 'claude-desktop',
    [ValidateSet('health','read','deploy')][string]$Scope = 'deploy',
    [string]$Bearer,
    [switch]$WithToken,
    [string]$BindHost = '127.0.0.1',
    [int]$Port = 8766,
    [switch]$NativeHttp,
    [switch]$WriteConfig,
    [string]$ServerName = 'ftx-mcp',
    [string]$ConfigPath,
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }

function Ok($m)   { Write-Host "ok: $m" -ForegroundColor Green }
function Info($m) { Write-Host $m -ForegroundColor Cyan }
function Warn($m) { Write-Host "WARN: $m" -ForegroundColor Yellow }

# 1. Token: only when auth is actually enabled (or explicitly requested).
# Auth is OFF by default on the loopback install - the service ignores the
# Authorization header entirely, so embedding a token just confuses operators.
$authOn = $false
foreach ($lvl in 'Process','User','Machine') {
    $v = [Environment]::GetEnvironmentVariable('FTX_AUTH_REQUIRED', $lvl)
    if ($v) { $authOn = ($v.Trim().ToLower() -notin @('0','false')); break }
}
if ($Bearer) {
    Ok "reusing supplied bearer"
} elseif ($authOn -or $WithToken) {
    Info "Issuing a '$Scope'-scoped token (label '$Label')..."
    $issue = & (Join-Path $PSScriptRoot 'issue-token.ps1') -Label $Label -Scope $Scope -Json -RepoRoot $RepoRoot
    if ($LASTEXITCODE -ne 0 -or -not $issue) { throw "issue-token.ps1 failed" }
    $tok = $issue | ConvertFrom-Json
    $Bearer = $tok.bearer
    Ok "token issued (id=$($tok.id), scope=$($tok.scope))"
} else {
    Ok "auth is off (FTX_AUTH_REQUIRED not set) - no token needed; emitting a tokenless config"
}

$url = "http://${BindHost}:$Port/mcp"

# 2. Build the server block.
# DEFAULT = stdio mcp-remote wrapped in `cmd /c`. Two Windows realities force this:
#   (a) The MSIX/Store Claude Desktop build silently ignores the native
#       `type:http` config form (it only loads stdio `command` servers).
#   (b) Spawning `npx` directly fails with "'C:\Program' is not recognized"
#       because npx is a .cmd/.ps1 shim and the "C:\Program Files" path splits on
#       the space. `cmd /c npx ...` resolves the shim + PATH correctly.
# -NativeHttp emits the dependency-free `type:http` form for Desktop builds that
# DO support it (validated to connect over raw HTTP; just not surfaced by the
# Store build's config loader).
if ($NativeHttp) {
    $server = [ordered]@{
        type = 'http'
        url  = $url
    }
    if ($Bearer) { $server.headers = [ordered]@{ Authorization = "Bearer $Bearer" } }
} else {
    # The cmd/c npx form silently requires Node.js at CONNECT time, not now:
    # without it the client shows a dead "ftx-mcp" entry with no useful error.
    # Warn here, where the operator can still fix it before restarting the app.
    if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
        Write-Host ("WARN: npx (Node.js) not found on PATH. The written config uses " +
            "'cmd /c npx -y mcp-remote' and will fail to connect until Node.js is " +
            "installed:  winget install OpenJS.NodeJS.LTS") -ForegroundColor Yellow
    }
    $mrArgs = @('/c','npx','-y','mcp-remote', $url)
    if ($Bearer) { $mrArgs += @('--header', "Authorization: Bearer $Bearer") }
    $server = [ordered]@{
        command = 'cmd'
        args    = $mrArgs
    }
}

# 3. Write/merge the config, or print it for manual paste.
# Config path: the Microsoft Store (MSIX) build of Claude Desktop redirects
# %APPDATA% into its package container, so it reads the config from
# %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude -- NOT plain
# %APPDATA%\Claude. Prefer the packaged path when the Store build is installed;
# fall back to %APPDATA% for the standalone .exe build. -ConfigPath overrides.
if ($ConfigPath) {
    $cfgPath = $ConfigPath
} else {
    $pkg = Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -EA SilentlyContinue |
        Where-Object { $_.Name -like 'Claude_*' } | Select-Object -First 1
    if ($pkg) {
        $cfgPath = Join-Path $pkg.FullName 'LocalCache\Roaming\Claude\claude_desktop_config.json'
    } else {
        $cfgPath = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
    }
}
Info "Claude Desktop config: $cfgPath"
if ($WriteConfig) {
    $cfgDir = Split-Path $cfgPath -Parent
    if (-not (Test-Path $cfgDir)) {
        Warn "Claude config dir not found ($cfgDir) - is Claude Desktop installed on THIS machine?"
        New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null
        Ok "created $cfgDir (will be picked up when Claude Desktop is installed here)"
    }
    if ((Test-Path $cfgPath) -and (Get-Content $cfgPath -Raw).Trim()) {
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
    } else {
        $cfg = [pscustomobject]@{}
    }
    # ensure mcpServers exists (indexer returns $null when absent -- StrictMode-safe)
    if (-not $cfg.PSObject.Properties['mcpServers']) {
        $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    # overwrite just our server, preserve any others
    $cfg.mcpServers | Add-Member -NotePropertyName $ServerName -NotePropertyValue ([pscustomobject]$server) -Force
    $json = $cfg | ConvertTo-Json -Depth 20
    $tmp = "$cfgPath.tmp"
    [System.IO.File]::WriteAllText($tmp, $json, [System.Text.UTF8Encoding]::new($false))
    Move-Item -Path $tmp -Destination $cfgPath -Force
    Ok ("wrote server '" + $ServerName + "' into " + $cfgPath)
    Info ("Restart Claude Desktop; the server appears under tools. Auto-bridges into Cowork too.")
} else {
    Info "Paste this into Claude Desktop, Settings, Developer, Edit Config:"
    ([pscustomobject]@{ mcpServers = [pscustomobject]@{ $ServerName = [pscustomobject]$server } } |
        ConvertTo-Json -Depth 20)
    Info "(Re-run with -WriteConfig to write it for you.)"
}

Write-Host ""
Info ("Local model: the service, Studio, and Claude Desktop all run on this machine.")
Info ("The service stays on " + $BindHost + " (loopback) - nothing is exposed on the network.")
