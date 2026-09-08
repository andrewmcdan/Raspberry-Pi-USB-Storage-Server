param([string]$PublicUrl = 'https://piusb.example.internal')
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    docker build -f manager/Dockerfile -t piusb-manager:local .
    if ($LASTEXITCODE -ne 0) { throw 'Manager image build failed' }
    if (-not (Test-Path -LiteralPath '.env')) {
        docker run --rm -it --user 0 --mount "type=bind,source=$PSScriptRoot,target=/work" --workdir /work piusb-manager:local python -m manager.configure --url $PublicUrl
        if ($LASTEXITCODE -ne 0) { throw 'Manager configuration failed' }
    }
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw 'Manager startup failed' }
    Write-Host 'Manager is listening on 127.0.0.1:8000. Configure HTTPS using docs/manager.md before connecting Pis.'
} finally { Pop-Location }
