<#
.SYNOPSIS
    DEVELOPER TOOL — Refresh the vendored BCQuality knowledge snapshot.

.DESCRIPTION
    Clones microsoft/BCQuality at a specified commit (or latest main), copies
    the knowledge files (*.md + *.good.al + *.bad.al) into the bc-agentic-mcp
    package-data directory, and writes MANIFEST.json.

    Regular users never run this script. The vendored snapshot ships with
    pip install and is always present. Run this when you want to upgrade the
    BCQuality corpus to a newer upstream commit.

    After running, review the diff and commit the updated vendor files.

.PARAMETER Commit
    Specific commit SHA or branch to pin.  Defaults to HEAD of main.

.PARAMETER VendorDir
    Target directory for the snapshot.  Defaults to the package-data path
    relative to this script's location.

.EXAMPLE
    .\Scripts\Update-BCQuality.ps1
    .\Scripts\Update-BCQuality.ps1 -Commit abc1234
#>
param(
    [string]$Commit = "",
    [string]$VendorDir = ""
)

$ErrorActionPreference = "Stop"

$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$defaultDst  = Join-Path $scriptDir "..\src\bc_agentic_mcp\vendor\BCQuality"
$dst         = if ($VendorDir) { $VendorDir } else { (Resolve-Path $defaultDst -ErrorAction SilentlyContinue) ?? $defaultDst }

$tmpDir = Join-Path $env:TEMP "BCQuality-update-$(Get-Random)"
Write-Host "[BCQuality] Cloning microsoft/BCQuality..."
git clone --depth=1 https://github.com/microsoft/BCQuality $tmpDir

if ($Commit) {
    Write-Host "[BCQuality] Checking out $Commit..."
    git -C $tmpDir fetch --depth=1 origin $Commit
    git -C $tmpDir checkout FETCH_HEAD
}

$sha = (git -C $tmpDir rev-parse HEAD).Trim()
Write-Host "[BCQuality] HEAD = $sha"

# Wipe existing vendor and re-populate
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
New-Item -ItemType Directory -Force -Path $dst | Out-Null

foreach ($layer in "microsoft", "community", "custom") {
    $srcKb = "$tmpDir\$layer\knowledge"
    if (Test-Path $srcKb) {
        New-Item -ItemType Directory -Force -Path "$dst\$layer\knowledge" | Out-Null
        Copy-Item -Recurse -Force "$srcKb\*" "$dst\$layer\knowledge\"
    }
}

$count = (Get-ChildItem -Recurse $dst -Filter "*.md").Count
$manifest = "{`"source`":`"https://github.com/microsoft/BCQuality`",`"commit`":`"$sha`",`"bundled_at`":`"$(Get-Date -Format 'yyyy-MM-dd')`",`"article_count`":$count}"
Set-Content "$dst\MANIFEST.json" -Value $manifest -Encoding utf8NoBOM

Remove-Item -Recurse -Force $tmpDir

Write-Host "[BCQuality] Done: $count knowledge articles at SHA $($sha.Substring(0,12))"
Write-Host "[BCQuality] Review the diff, then commit vendor/BCQuality to lock this version."
