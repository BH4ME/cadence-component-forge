[CmdletBinding()]
param(
    [string]$ViewerRoot = (Join-Path $env:USERPROFILE '.codex\skills\cad-viewer'),
    [string]$CadPython = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
)

$ErrorActionPreference = 'Stop'
$ViewerUrl = 'http://127.0.0.1:4178/'
try {
    $Info = Invoke-RestMethod -Uri ($ViewerUrl + '__cad/server') -TimeoutSec 3
    if ($Info.app -eq 'cad-viewer' -and $Info.dynamicRoot -and $Info.serverApiVersion -ge 2) {
        Write-Host "CAD Viewer is available: $ViewerUrl"
        return
    }
} catch {}
$Server = Join-Path $ViewerRoot 'scripts\viewer\backend\server.mjs'
if (-not (Test-Path -LiteralPath $Server -PathType Leaf)) { throw "CAD Viewer runtime not found: $Server" }
if (-not (Test-Path -LiteralPath $CadPython -PathType Leaf)) { throw "Specify -CadPython with a Python installation containing cadpy and OCP." }
& $CadPython -c 'import cadpy, OCP'
if ($LASTEXITCODE -ne 0) { throw 'The selected CAD Python cannot import cadpy and OCP.' }
$Node = (Get-Command node.exe -ErrorAction Stop).Source
$LogDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'jlc_library\.capture_jobs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Previous = $env:VIEWER_CAD_PYTHON
try {
    $env:VIEWER_CAD_PYTHON = $CadPython
    $Arguments = @(('"' + $Server + '"'), '--host', '127.0.0.1', '--shutdown-after', '12h')
    $Process = Start-Process -FilePath $Node -ArgumentList $Arguments -WorkingDirectory $ViewerRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir 'viewer.log') -RedirectStandardError (Join-Path $LogDir 'viewer_error.log') -PassThru
} finally {
    $env:VIEWER_CAD_PYTHON = $Previous
}
for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
    Start-Sleep -Milliseconds 250
    $Process.Refresh()
    if ($Process.HasExited) { throw "Viewer exited. Inspect $(Join-Path $LogDir 'viewer_error.log')" }
    try {
        $Info = Invoke-RestMethod -Uri ($ViewerUrl + '__cad/server') -TimeoutSec 1
        if ($Info.app -eq 'cad-viewer' -and $Info.dynamicRoot -and $Info.serverApiVersion -ge 2) {
            Write-Host "CAD Viewer started: $ViewerUrl (12 hour lifetime)"
            return
        }
    } catch {}
}
throw "Viewer did not become available. Inspect $LogDir."
