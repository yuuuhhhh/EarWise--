param([int]$Port = 8765, [switch]$NoBrowser)
& (Join-Path $PSScriptRoot 'start.ps1') -Simulate -Port $Port -NoBrowser:$NoBrowser
exit $LASTEXITCODE
