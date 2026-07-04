<#
.SYNOPSIS
    One-command installer for the bc-agentic-mcp kit (server + agents + instructions).

.DESCRIPTION
    Run from an unzipped bc-agentic-mcp-kit folder:

        .\install.ps1 -TargetRepo "C:\src\MyALRepo" [-SpecsRoot "C:\bc-workspaces"] [-Python "py -3.12"]

    Steps performed:
      1. Verifies Python >= 3.10
      2. Creates a dedicated venv next to the kit (.bc-agentic-venv) and installs the server
      3. Copies agent files into <TargetRepo>\.github\agents\
      4. Copies instruction files into <TargetRepo>\.github\instructions\
      5. Writes mcp.json.generated with ready-to-paste VS Code MCP registration
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetRepo,

    [string]$SpecsRoot = "",

    [string]$OrgUrl = "",

    [string]$Project = "",

    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$kitRoot = $PSScriptRoot

function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# --- 1. sanity checks ---------------------------------------------------
if (-not (Test-Path $TargetRepo)) { Fail "TargetRepo '$TargetRepo' does not exist." }
if (-not (Test-Path (Join-Path $kitRoot "server\pyproject.toml"))) {
    Fail "Run install.ps1 from inside the unzipped kit (server\pyproject.toml not found)."
}
$pyVersion = & $Python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))" 2>$null
if (-not $pyVersion) { Fail "Python not found via '$Python'. Install Python 3.10+ or pass -Python." }
if ([version]$pyVersion -lt [version]"3.10") { Fail "Python $pyVersion found; 3.10+ required." }
Write-Host "[1/5] Python $pyVersion OK" -ForegroundColor Green

# --- 2. venv + install ---------------------------------------------------
$venv = Join-Path $kitRoot ".bc-agentic-venv"
if (-not (Test-Path $venv)) { & $Python -m venv $venv }
$venvPython = Join-Path $venv "Scripts\python.exe"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet (Join-Path $kitRoot "server")
if ($LASTEXITCODE -ne 0) { Fail "pip install failed." }
Write-Host "[2/5] Server installed into $venv" -ForegroundColor Green

# --- 3. agent files ------------------------------------------------------
$agentsDir = Join-Path $TargetRepo ".github\agents"
New-Item -ItemType Directory -Force -Path $agentsDir | Out-Null
Get-ChildItem (Join-Path $kitRoot "agents\*.agent.md") | ForEach-Object {
    Copy-Item $_.FullName -Destination $agentsDir -Force
    Write-Host "      + .github\agents\$($_.Name)"
}
Write-Host "[3/5] Agent files installed" -ForegroundColor Green

# --- 4. instruction files -------------------------------------------------
$instrDir = Join-Path $TargetRepo ".github\instructions"
New-Item -ItemType Directory -Force -Path $instrDir | Out-Null
Get-ChildItem (Join-Path $kitRoot "instructions\*.instructions.md") | ForEach-Object {
    Copy-Item $_.FullName -Destination $instrDir -Force
    Write-Host "      + .github\instructions\$($_.Name)"
}
Write-Host "[4/5] Instruction files installed" -ForegroundColor Green

# --- 5. mcp.json snippet ----------------------------------------------------
if ($SpecsRoot) { New-Item -ItemType Directory -Force -Path $SpecsRoot | Out-Null }
$serverArgs = @("-m", "bc_agentic_mcp.server", "--project-root", "$TargetRepo")
if ($SpecsRoot) { $serverArgs += @("--specs-root", "$SpecsRoot") }
$serverDef = [ordered]@{
    type    = "stdio"
    command = "$venvPython"
    args    = $serverArgs
}
if ($OrgUrl -or $Project) {
    $envMap = [ordered]@{}
    if ($OrgUrl)  { $envMap["AZURE_DEVOPS_ORG"] = $OrgUrl }
    if ($Project) { $envMap["AZURE_DEVOPS_PROJECT"] = $Project }
    $serverDef["env"] = $envMap
}
$snippet = @{ servers = @{ "bc-agentic-mcp" = $serverDef } } | ConvertTo-Json -Depth 6
$snippetPath = Join-Path $kitRoot "mcp.json.generated"
Set-Content -Path $snippetPath -Value $snippet -Encoding UTF8
Write-Host "[5/5] MCP registration written to $snippetPath" -ForegroundColor Green

Write-Host ""
Write-Host "=== DONE ===" -ForegroundColor Cyan
Write-Host "1. Merge mcp.json.generated into your VS Code mcp.json (Ctrl+Shift+P -> 'MCP: Open User Configuration')"
Write-Host "2. Set your Azure DevOps PAT (never stored in files):  `$env:AZURE_DEVOPS_EXT_PAT = '<your PAT>'  (or set it machine-wide)"
Write-Host "3. Reload VS Code -> the bc_* tools appear; drive them with the bc-orchestrator agent"
Write-Host "4. Optional web cockpit:"
$specsPart = ""
if ($SpecsRoot) { $specsPart = " --specs-root `"$SpecsRoot`"" }
Write-Host "   $venvPython -m bc_agentic_mcp.mission_control.app --project-root `"$TargetRepo`"$specsPart --open"
