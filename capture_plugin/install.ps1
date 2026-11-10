[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$CadenceRoot = '',
    [Alias('PythonExe')]
    [string]$Python,
    [string]$LibraryRoot,
    [string]$ViewerBaseUrl = '',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$PluginDir = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $PluginDir
$AutoLoadDir = Join-Path $CadenceRoot 'tools\capture\tclscripts\capAutoLoad'
$InitPath = Join-Path $AutoLoadDir 'jlc2capture_init.tcl'
$ConfigPath = Join-Path $PluginDir 'config.json'
$OwnerMarker = '# jlc2capture managed initializer v1'

if (-not (Test-Path -LiteralPath $AutoLoadDir -PathType Container)) {
    throw "Capture autoload directory not found: $AutoLoadDir"
}
if (Test-Path -LiteralPath $InitPath) {
    $Existing = Get-Content -LiteralPath $InitPath -Raw
    if (-not $Existing.StartsWith($OwnerMarker)) {
        throw "Refusing to replace an unrelated initializer: $InitPath"
    }
}
if ($Uninstall) {
    if ((Test-Path -LiteralPath $InitPath) -and $PSCmdlet.ShouldProcess($InitPath, 'Remove JLC2Capture initializer')) {
        Remove-Item -LiteralPath $InitPath
    }
    Write-Host 'JLC2Capture autoload removed. Library files and configuration are retained.'
    return
}
if (-not $Python) {
    $ProjectPython = Join-Path $ProjectRoot '.venv-capture\Scripts\python.exe'
    if (Test-Path -LiteralPath $ProjectPython -PathType Leaf) {
        $Python = $ProjectPython
    } else {
        $Python = (Get-Command python.exe -ErrorAction Stop).Source
    }
}
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not $LibraryRoot) { $LibraryRoot = Join-Path $ProjectRoot 'jlc_library' }
$LibraryRoot = [IO.Path]::GetFullPath($LibraryRoot)
if (-not $CadenceRoot) { throw 'Pass -CadenceRoot with the installed Cadence root, for example C:\Cadence\SPB_17.4' }
$CadenceRoot = (Resolve-Path -LiteralPath $CadenceRoot).Path
$PluginPath = Join-Path $PluginDir 'jlc2capture.tcl'
if (-not (Test-Path -LiteralPath $PluginPath)) { throw "Missing plugin: $PluginPath" }

$Config = [ordered]@{
    python = $Python
    library_root = $LibraryRoot
    cadence_root = $CadenceRoot
    viewer_base_url = $ViewerBaseUrl
}
$EncodedPath = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PluginPath))
$Initializer = @"
$OwnerMarker
if {[catch {
    source -encoding utf-8 [encoding convertfrom utf-8 [binary decode base64 {$EncodedPath}]]
} jlc2captureError]} {
    puts "JLC2Capture initialization failed: `$jlc2captureError"
}
"@
$Utf8 = New-Object Text.UTF8Encoding($false)
if ($PSCmdlet.ShouldProcess($ConfigPath, 'Configure JLC2Capture')) {
    if (Test-Path -LiteralPath $ConfigPath) {
        $BackupPath = "$ConfigPath.backup_$(Get-Date -Format 'yyyyMMdd_HHmmss_ffff')"
        Copy-Item -LiteralPath $ConfigPath -Destination $BackupPath
    }
    [IO.File]::WriteAllText($ConfigPath, ($Config | ConvertTo-Json), $Utf8)
}
if ($PSCmdlet.ShouldProcess($InitPath, 'Install JLC2Capture autoload initializer')) {
    [IO.File]::WriteAllText($InitPath, $Initializer, $Utf8)
}
if ($WhatIfPreference) {
    Write-Host "Installation preview: $InitPath"
} else {
    Write-Host "Installed: $InitPath"
}
Write-Host "Library folder: $LibraryRoot"
$DisplayPath = $PluginPath.Replace('\', '/')
Write-Host "Load in an existing Capture session: source -encoding utf-8 {$DisplayPath}"
Write-Host 'Menu after loading: JLC/LCSC > Search Components...'
