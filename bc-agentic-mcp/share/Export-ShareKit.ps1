<#
.SYNOPSIS
    Export a self-contained, shareable bc-agentic-mcp kit (zip).

.DESCRIPTION
    Collects the MCP server package, agent files, instruction files and the
    installer into one folder and zips it. Give the zip to anyone — they run
    install.ps1 and are up in minutes.

        .\Export-ShareKit.ps1 [-OutputDir "C:\temp"]

    Sources:
      server        -> this project (pyproject.toml, README.md, src/, tests/, agents/)
      agents        -> agents/*.agent.md + workspace .github/agents/bc-orchestrator.agent.md
      instructions  -> workspace .github/instructions/bc-gates.instructions.md
                       + user .copilot/instructions/bc-mcp-routing.instructions.md (if present)
#>
param(
    [string]$OutputDir = (Join-Path ([System.IO.Path]::GetTempPath()) "bc-agentic-mcp-kit")
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot          # inner bc-agentic-mcp project
$workspaceRoot = (Resolve-Path (Join-Path $projectRoot "..\..")).Path  # Brain workspace

$kit = Join-Path $OutputDir "bc-agentic-mcp-kit"
if (Test-Path $kit) { Remove-Item $kit -Recurse -Force }
New-Item -ItemType Directory -Force -Path $kit | Out-Null

# --- 1. server package (lean copy, no caches) -----------------------------
$server = Join-Path $kit "server"
New-Item -ItemType Directory -Force -Path $server | Out-Null
Copy-Item (Join-Path $projectRoot "pyproject.toml") $server
Copy-Item (Join-Path $projectRoot "README.md") $server
Copy-Item (Join-Path $projectRoot "src") (Join-Path $server "src") -Recurse
Copy-Item (Join-Path $projectRoot "tests") (Join-Path $server "tests") -Recurse
Copy-Item (Join-Path $projectRoot "agents") (Join-Path $server "agents") -Recurse
Get-ChildItem $server -Recurse -Directory |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache") -or $_.Name -like "*.egg-info" } |
    Remove-Item -Recurse -Force
Write-Host "[1/4] server/ packaged" -ForegroundColor Green

# --- 2. agent files ---------------------------------------------------------
$agents = Join-Path $kit "agents"
New-Item -ItemType Directory -Force -Path $agents | Out-Null
Copy-Item (Join-Path $projectRoot "agents\*.agent.md") $agents
$orchestrator = Join-Path $workspaceRoot ".github\agents\bc-orchestrator.agent.md"
if (Test-Path $orchestrator) { Copy-Item $orchestrator $agents }
else { Write-Warning "bc-orchestrator.agent.md not found at $orchestrator — kit ships without it." }
Write-Host "[2/4] agents/ packaged ($((Get-ChildItem $agents).Count) files)" -ForegroundColor Green

# --- 3. instruction files ----------------------------------------------------
$instr = Join-Path $kit "instructions"
New-Item -ItemType Directory -Force -Path $instr | Out-Null
$candidates = @(
    (Join-Path $workspaceRoot ".github\instructions\bc-gates.instructions.md"),
    (Join-Path $env:USERPROFILE ".copilot\instructions\bc-mcp-routing.instructions.md")
)
foreach ($file in $candidates) {
    if (Test-Path $file) { Copy-Item $file $instr }
    else { Write-Warning "instruction file not found: $file" }
}
Write-Host "[3/4] instructions/ packaged ($((Get-ChildItem $instr).Count) files)" -ForegroundColor Green

# --- 4. installer + docs + zip ------------------------------------------------
Copy-Item (Join-Path $PSScriptRoot "install.ps1") $kit
Copy-Item (Join-Path $PSScriptRoot "INSTALL.md") $kit
$stamp = Get-Date -Format "yyyyMMdd"
$zip = Join-Path $OutputDir "bc-agentic-mcp-kit-$stamp.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $kit -DestinationPath $zip
Write-Host "[4/4] Kit zipped" -ForegroundColor Green

Write-Host ""
Write-Host "=== SHARE KIT READY ===" -ForegroundColor Cyan
Write-Host "Folder: $kit"
Write-Host "Zip:    $zip"
Write-Host "Recipient: unzip -> .\install.ps1 -TargetRepo <AL repo> [-SpecsRoot <dir>]"
