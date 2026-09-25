param([switch]$Simulate, [int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
try {
    & (Join-Path $PSScriptRoot 'setup.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Push-Location -LiteralPath $PSScriptRoot
    try {
        $launchArguments = @('-m', 'app.main', '--port', "$Port")
        if ($Simulate) { $launchArguments += '--simulate' }
        if ($NoBrowser) { $launchArguments += '--no-browser' }
        & (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') @launchArguments
        $launchExitCode = $LASTEXITCODE
    } finally { Pop-Location }
    exit $launchExitCode
} catch {
    Write-Host "EarWise startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
