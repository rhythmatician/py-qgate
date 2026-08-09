[CmdletBinding()]
param(
    [string] $Destination = (
        Join-Path ([Environment]::GetFolderPath('UserProfile')) '.agents\skills\adopt-qgate'
    )
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repositoryRoot 'skills\adopt-qgate'
$skillFile = Join-Path $source 'SKILL.md'

if (-not (Test-Path -LiteralPath $skillFile -PathType Leaf)) {
    throw "Canonical skill not found at '$skillFile'."
}

$source = (Resolve-Path -LiteralPath $source).Path
$destination = [IO.Path]::GetFullPath($Destination)
$destinationParent = Split-Path -Parent $destination

if (Test-Path -LiteralPath $destination) {
    $existing = Get-Item -LiteralPath $destination -Force
    $existingTarget = @($existing.Target) | Select-Object -First 1
    if ($existing.LinkType -eq 'Junction' -and $existingTarget) {
        $resolvedTarget = [IO.Path]::GetFullPath($existingTarget)
        if ($resolvedTarget -eq $source) {
            Write-Host "adopt-qgate is already linked at '$destination'."
            exit 0
        }
    }

    throw "Destination already exists and is not the expected junction: '$destination'."
}

New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
New-Item -ItemType Junction -Path $destination -Target $source | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $destination 'SKILL.md') -PathType Leaf)) {
    throw "Junction was created, but SKILL.md is not reachable at '$destination'."
}

Write-Host "Linked '$destination' to '$source'."
