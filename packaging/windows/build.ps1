[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$Output = '',
    [string]$Iscc = '',
    [string]$UpdateManifestUrl = ''
)
$ErrorActionPreference = 'Stop'
$BuildRoot = $PSScriptRoot
$ProjectRoot = Split-Path (Split-Path $BuildRoot -Parent) -Parent
$UpdateManifestUrl = $UpdateManifestUrl.Trim()
if ($UpdateManifestUrl) {
    $ParsedUpdateUrl = $null
    $InvalidUpdateUrl = (
        -not [Uri]::TryCreate($UpdateManifestUrl, [UriKind]::Absolute, [ref]$ParsedUpdateUrl) -or
        $ParsedUpdateUrl.Scheme -ne 'https' -or
        [string]::IsNullOrEmpty($ParsedUpdateUrl.Host) -or
        $ParsedUpdateUrl.UserInfo -or
        $ParsedUpdateUrl.Fragment -or
        [regex]::IsMatch($UpdateManifestUrl, '[\s''"\\]')
    )
    if ($InvalidUpdateUrl) {
        throw 'UpdateManifestUrl must be an absolute HTTPS URL without credentials, fragments, whitespace, quotes, or backslashes'
    }
}
if (-not $Python) { $Python = Join-Path $ProjectRoot '.venv-capture\Scripts\python.exe' }
if (-not $Output) { $Output = Join-Path $BuildRoot ('dist\build_' + (Get-Date -Format 'yyyyMMdd_HHmmss')) }
$Output = [IO.Path]::GetFullPath($Output)
& $Python (Join-Path $BuildRoot 'build.py') --output $Output
if ($LASTEXITCODE -ne 0) { throw 'Payload build failed' }
$Info = Get-Content (Join-Path $Output 'build-info.json') -Raw | ConvertFrom-Json
$EmbeddedPython = Join-Path $Output 'payload\runtime\python.exe'
$Signature = Get-AuthenticodeSignature -LiteralPath $EmbeddedPython
if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
    throw 'Official embedded Python signature validation failed'
}
if (-not $Iscc) {
    $Existing = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($Existing) { $Iscc = $Existing.Source }
}
if (-not $Iscc) {
    $ToolRoot = Join-Path $BuildRoot 'tools\inno-6.4.3'
    $Installer = Join-Path $BuildRoot 'tools\downloads\innosetup-6.4.3.exe'
    if (-not (Test-Path -LiteralPath $Installer)) {
        New-Item -ItemType Directory -Path (Split-Path $Installer -Parent) -Force | Out-Null
        Invoke-WebRequest -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-6_4_3/innosetup-6.4.3.exe' -OutFile $Installer
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Installer
    $CompilerHash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash
    if ($CompilerHash -ne 'F3C42116542C4CC57263C5BA6C4FEABFC49FE771F2F98A79D2F7628B8762723B') {
        throw 'Inno Setup checksum differs from the official 6.4.3 .issig file'
    }
    if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notmatch 'Pyrsys B\.V\.|JRSoftware|Jordan Russell') {
        throw 'Official Inno Setup signature validation failed'
    }
    $CompilerInstall = Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', ('/DIR="' + $ToolRoot + '"')) -PassThru -Wait -WindowStyle Hidden
    if ($CompilerInstall.ExitCode -ne 0) { throw 'Inno Setup compiler installation failed' }
    $Iscc = Join-Path $ToolRoot 'ISCC.exe'
}
$IsccArguments = @(
    '/Q'
    ('/DPayloadDir=' + (Join-Path $Output 'payload'))
    ('/DReleaseId=' + $Info.release_id)
    ('/DAppVersion=' + $Info.version)
    ('/DOutputDirectory=' + $Output)
)
if ($UpdateManifestUrl) { $IsccArguments += ('/DUpdateManifestUrl=' + $UpdateManifestUrl) }
$IsccArguments += (Join-Path $BuildRoot 'setup.iss')
& $Iscc @IsccArguments
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed' }
Get-ChildItem -LiteralPath $Output -Filter '*-Setup.exe' | Get-FileHash -Algorithm SHA256
