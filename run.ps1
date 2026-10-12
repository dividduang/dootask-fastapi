param(
    [ValidateSet('serve', 'init-db', 'secrets')]
    [string]$Command = 'serve',
    [string]$ListenHost = '127.0.0.1',
    [int]$Port = 18080
)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root '.venv/Scripts/python.exe'
if (!(Test-Path $Python)) { throw 'Create .venv with py -3.12 -m venv .venv and install requirements.lock first.' }
if ($Command -eq 'secrets') {
    $File = Join-Path $Root '.runtime/backend.env'
    if (Test-Path $File) { throw 'Refusing to overwrite existing backend secrets.' }
    New-Item -ItemType Directory -Path (Split-Path $File) -Force | Out-Null
    $Bytes = New-Object byte[] 32
    $Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $Rng.GetBytes($Bytes)
    $Csrf = [Convert]::ToBase64String($Bytes)
    $Rng.GetBytes($Bytes)
    $Pgp = [Convert]::ToBase64String($Bytes)
    $Rng.Dispose()
    [IO.File]::WriteAllText($File, "CSRF_SECRET=$Csrf`nPGP_STORAGE_KEY=$Pgp`n", [Text.UTF8Encoding]::new($false))
    Write-Output 'Generated private .runtime/backend.env; restrict this directory to the service account.'
    exit 0
}
foreach ($Name in @('runtime.env', 'backend.env')) {
    $File = Join-Path $Root ".runtime/$Name"
    if (!(Test-Path $File)) { throw "Missing $File" }
    foreach ($Line in [IO.File]::ReadAllLines($File)) {
        if ($Line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
}
if ($Command -eq 'init-db') {
    & $Python -m backend.manage init-db
} else {
    # Direct loopback only. Trusted-proxy deployment must explicitly configure allowed proxy IPs.
    & $Python -m uvicorn backend.main:create_app --factory --host $ListenHost --port $Port --no-proxy-headers --no-access-log
}
exit $LASTEXITCODE
