<#
.SYNOPSIS
Checks or installs the dependency scope selected for a music-organiser flow.

.DESCRIPTION
The default is read-only. Use -Install only after the user has selected the
mode and approved the scoped environment change. Local models and llama.cpp
binaries are never downloaded automatically.
#>

[CmdletBinding()]
param(
    [ValidateSet("base", "metadata-provider", "metadata-local", "cues-local", "all")]
    [string]$Mode = "base",
    [switch]$Install,
    [string]$CueEngineRoot = $env:MUSIC_CUE_ENGINE_ROOT,
    [string]$LlamaRoot = $env:MUSIC_LLAMA_ROOT,
    [string]$LlamaRuntime = $env:MUSIC_LLAMA_RUNTIME,
    [string]$MetadataModel = $env:MUSIC_METADATA_MODEL
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$checks = [System.Collections.Generic.List[object]]::new()

if ([string]::IsNullOrWhiteSpace($CueEngineRoot)) {
    $CueEngineRoot = Join-Path $project ".runtime\cue-engine"
}
if ([string]::IsNullOrWhiteSpace($LlamaRuntime) -and -not [string]::IsNullOrWhiteSpace($LlamaRoot)) {
    $LlamaRuntime = Join-Path $LlamaRoot "llama-server.exe"
}

function Add-Check {
    param([string]$Name, [bool]$Present, [string]$Detail)
    $checks.Add([pscustomobject]@{ Name = $Name; Present = $Present; Detail = $Detail })
}

function Test-CommandPresent {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

$needsBase = $Mode -in @("base", "metadata-provider", "metadata-local", "cues-local", "all")
$needsLocalMetadata = $Mode -in @("metadata-local", "all")
$needsLocalCues = $Mode -in @("cues-local", "all")

if ($needsBase) {
    $pythonPresent = Test-CommandPresent "python"
    Add-Check "python" $pythonPresent "Required for organiser scripts"
    Add-Check "requirements.txt" (Test-Path -LiteralPath (Join-Path $project "requirements.txt") -PathType Leaf) "Pinned base requirements"

    $venvPython = Join-Path $project ".venv\Scripts\python.exe"
    if ($Install) {
        if (-not $pythonPresent) {
            throw "Python is missing; install a supported Python runtime before using -Install."
        }
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            & python -m venv (Join-Path $project ".venv")
            if ($LASTEXITCODE -ne 0) { throw "Could not create the project virtual environment." }
        }
        & $venvPython -m pip install -r (Join-Path $project "requirements.txt")
        if ($LASTEXITCODE -ne 0) { throw "Base dependency installation failed." }
    }
    Add-Check "project environment" (Test-Path -LiteralPath $venvPython -PathType Leaf) $venvPython
}

if ($Mode -eq "metadata-provider") {
    Add-Check "local metadata model" $true "Not required; the active Claude or Codex provider is selected"
}

if ($needsLocalMetadata) {
    Add-Check "nvidia-smi" (Test-CommandPresent "nvidia-smi.exe") "Used only for hardware and active-job inspection"
    $llamaLauncher = if ([string]::IsNullOrWhiteSpace($LlamaRoot)) { $null } else { Join-Path $LlamaRoot "local-genai.ps1" }
    Add-Check "llama launcher" ($null -ne $llamaLauncher -and (Test-Path -LiteralPath $llamaLauncher -PathType Leaf)) ([string]$llamaLauncher)
    Add-Check "llama server" (-not [string]::IsNullOrWhiteSpace($LlamaRuntime) -and (Test-Path -LiteralPath $LlamaRuntime -PathType Leaf)) $LlamaRuntime
    Add-Check "metadata model" (-not [string]::IsNullOrWhiteSpace($MetadataModel) -and (Test-Path -LiteralPath $MetadataModel -PathType Leaf)) $MetadataModel
    if ($Install -and @($checks | Where-Object { $_.Name -in @("llama launcher", "llama server", "metadata model") -and -not $_.Present }).Count) {
        throw "Local metadata assets are missing. They require a separately reviewed and approved install; this script will not download them automatically."
    }
}

if ($needsLocalCues) {
    $uvPresent = Test-CommandPresent "uv"
    $cueProject = Join-Path $CueEngineRoot "pyproject.toml"
    $cueLock = Join-Path $CueEngineRoot "uv.lock"
    $cueEngine = Join-Path $CueEngineRoot "src\autohotcue"
    Add-Check "uv" $uvPresent "Locked cue-engine environment manager"
    Add-Check "cue project" (Test-Path -LiteralPath $cueProject -PathType Leaf) $cueProject
    Add-Check "cue lockfile" (Test-Path -LiteralPath $cueLock -PathType Leaf) $cueLock
    Add-Check "cue engine" (Test-Path -LiteralPath $cueEngine -PathType Container) $cueEngine
    Add-Check "nvidia-smi" (Test-CommandPresent "nvidia-smi.exe") "Inspect hardware and preserve active GPU work"

    if ($Install) {
        if (-not $uvPresent) { throw "uv is missing; install or approve it before cue dependency setup." }
        if (-not (Test-Path -LiteralPath $cueProject -PathType Leaf) -or -not (Test-Path -LiteralPath $cueLock -PathType Leaf)) {
            throw "The configured cue engine or lockfile is missing: $CueEngineRoot. Supply a reviewed engine with MUSIC_CUE_ENGINE_ROOT before installing its environment."
        }
        Push-Location -LiteralPath $CueEngineRoot
        try {
            & uv sync --locked
            if ($LASTEXITCODE -ne 0) { throw "Cue dependency installation failed." }
        }
        finally {
            Pop-Location
        }
    }
}

$checks | Format-Table -AutoSize
$missing = @($checks | Where-Object { -not $_.Present })
if ($missing.Count) {
    Write-Warning ("Missing dependency checks: " + (($missing.Name | Sort-Object -Unique) -join ", "))
    exit 2
}

Write-Host "Dependency preflight passed for mode '$Mode'."
