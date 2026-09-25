param([string]$PythonPath, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskVenv = Join-Path $taskRoot '.venv'
$taskPython = Join-Path $taskVenv 'Scripts\python.exe'
$taskLock = Join-Path $taskRoot 'requirements-lock.txt'

function Test-EarWisePython([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    try {
        & $Candidate -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Find-EarWisePython {
    if ($PythonPath) {
        if (Test-EarWisePython $PythonPath) { return (Resolve-Path -LiteralPath $PythonPath).Path }
        throw '-PythonPath must point to a working 64-bit Python 3.12 interpreter.'
    }
    $candidates = @()
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $found = & $launcher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0) { $candidates += $found }
        } catch { }
    }
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\EarWise\Python312\python.exe')
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notlike '*\Microsoft\WindowsApps\*') {
        $candidates += $pythonCommand.Source
    }
    foreach ($candidate in $candidates) {
        if (Test-EarWisePython $candidate) { return $candidate }
    }
    return $null
}

function Install-EarWisePython {
    # Official CPython 3.12 Windows installer; current-user installation only.
    $installRoot = Join-Path $env:LOCALAPPDATA 'Programs\EarWise\Python312'
    if (Test-Path -LiteralPath $installRoot) {
        throw "An unusable Python installation exists at $installRoot. It was preserved. Repair it or use -PythonPath."
    }
    $cacheRoot = Join-Path $taskRoot 'work\setup'
    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
    $installer = Join-Path $cacheRoot 'python-3.12.10-amd64.exe'
    $installLog = Join-Path $cacheRoot 'python-install.log'
    Write-Host '[1/4] Downloading the official Python 3.12.10 installer...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $installer
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        throw 'Python installer signature verification failed. The installer was not executed.'
    }
    Write-Host '[1/4] Installing Python for the current Windows user...'
    $installArguments = @('/quiet', 'InstallAllUsers=0', 'Include_launcher=0', 'Include_test=0',
        'Include_doc=0', 'Include_tcltk=0', 'Include_pip=1', 'PrependPath=0', 'Shortcuts=0',
        ('TargetDir="{0}"' -f $installRoot), '/log', ('"{0}"' -f $installLog))
    $installation = Start-Process -FilePath $installer -ArgumentList $installArguments -WindowStyle Hidden -PassThru -Wait
    if ($installation.ExitCode -notin @(0, 3010)) {
        throw "Python installation failed (exit $($installation.ExitCode)). See $installLog"
    }
    $installedPython = Join-Path $installRoot 'python.exe'
    if (-not (Test-EarWisePython $installedPython)) {
        throw "Python installation could not be verified. See $installLog"
    }
    return $installedPython
}

try {
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
        throw 'This setup supports 64-bit Windows 10/11.'
    }
    if (-not (Test-Path -LiteralPath $taskLock -PathType Leaf)) {
        throw 'requirements-lock.txt is missing. Extract or clone the complete repository before running setup.'
    }
    Push-Location -LiteralPath $taskRoot
    try {
        Write-Host 'EarWise - environment setup'
        if (Test-Path -LiteralPath $taskVenv) {
            if (-not (Test-EarWisePython $taskPython)) {
                throw 'The existing .venv is broken or is not 64-bit Python 3.12. It was preserved. Rename it before rerunning setup.'
            }
            Write-Host '[1/4] Reusing the project Python 3.12 environment.'
        } else {
            if ($CheckOnly) { throw 'The project environment is missing. Run setup.bat first.' }
            $basePython = Find-EarWisePython
            if (-not $basePython) { $basePython = Install-EarWisePython }
            Write-Host "[1/4] Creating .venv with $basePython"
            & $basePython -m venv $taskVenv
            if ($LASTEXITCODE -ne 0) { throw 'Unable to create .venv. Check folder write permissions and free disk space.' }
        }
        Write-Host '[2/4] Checking pinned dependencies...'
        $environmentCheck = Join-Path $taskRoot 'scripts\check_environment.py'
        & $taskPython $environmentCheck --dependencies-only
        if ($LASTEXITCODE -ne 0) {
            if ($CheckOnly) { throw 'Dependencies are missing or differ from the lock file. Run setup.bat.' }
            & $taskPython -m pip --version | Out-Null
            if ($LASTEXITCODE -ne 0) {
                & $taskPython -m ensurepip --upgrade
                if ($LASTEXITCODE -ne 0) { throw 'Unable to initialize pip in .venv.' }
            }
            & $taskPython -m pip install --disable-pip-version-check --only-binary=:all: -r $taskLock
            if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the network/PyPI connection, then rerun setup.bat.' }
            & $taskPython $environmentCheck --dependencies-only
            if ($LASTEXITCODE -ne 0) { throw 'Installed dependencies do not match requirements-lock.txt.' }
        }
        Write-Host '[3/4] Checking package compatibility and application imports...'
        & $taskPython -m pip check
        if ($LASTEXITCODE -ne 0) { throw 'Dependency compatibility check failed.' }
        Write-Host '[4/4] Checking application, configuration and the 16 videos...'
        & $taskPython $environmentCheck
        if ($LASTEXITCODE -ne 0) { throw 'Application/configuration/media check failed. Read the error above.' }
        Write-Host ''
        Write-Host 'EarWise setup complete. No headset is required for simulation.' -ForegroundColor Green
        Write-Host 'Next: start-simulation.bat (simulation) or start.bat (real headset).'
    } finally { Pop-Location }
} catch {
    Write-Host "[FAILED] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
exit 0
