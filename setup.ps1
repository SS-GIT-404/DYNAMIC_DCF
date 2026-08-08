<#
.SYNOPSIS
    One-command setup for the DCF model on Windows.

.DESCRIPTION
    Creates a virtual environment and installs dependencies.

    Windows ships an "app execution alias" that intercepts `python` and sends
    you to the Microsoft Store, so `python` often is not a working command even
    when Python is installed. This script uses the `py` launcher, which is what
    the official Windows installer actually puts on PATH.

.EXAMPLE
    .\setup.ps1
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "Dynamic DCF Model - setup" -ForegroundColor Cyan
Write-Host ("-" * 40)

# --- locate a working interpreter ----------------------------------------- #
$launcher = $null
foreach ($candidate in @("py", "python3", "python")) {
    try {
        $version = & $candidate --version 2>&1
        # The Store stub "succeeds" but prints this instead of a version.
        if ($LASTEXITCODE -eq 0 -and $version -notmatch "was not found") {
            $launcher = $candidate
            Write-Host "Found interpreter: $candidate ($version)"
            break
        }
    } catch {
        continue
    }
}

if (-not $launcher) {
    Write-Host "No working Python found." -ForegroundColor Red
    Write-Host "Install Python 3.9+ from https://www.python.org/downloads/ and"
    Write-Host "tick 'Add python.exe to PATH' during installation."
    exit 1
}

# --- create the virtual environment --------------------------------------- #
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment (.venv)..."
    & $launcher -m venv .venv
} else {
    Write-Host "Virtual environment already exists - reusing it."
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Virtual environment looks broken. Delete .venv and re-run." -ForegroundColor Red
    exit 1
}

# --- install dependencies -------------------------------------------------- #
Write-Host "Installing dependencies (this takes a minute the first time)..."
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt --quiet

# --- SEC contact reminder --------------------------------------------------- #
if (-not $env:SEC_UA_EMAIL) {
    Write-Host ""
    Write-Host "Note: SEC_UA_EMAIL is not set." -ForegroundColor Yellow
    Write-Host "The SEC asks callers to identify themselves. Set it with:"
    Write-Host '    $env:SEC_UA_EMAIL = "you@example.com"' -ForegroundColor Gray
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Run any of these:" -ForegroundColor Cyan
Write-Host "    .\run.ps1 app              # interactive web app"
Write-Host "    .\run.ps1 value AAPL       # DCF in the terminal"
Write-Host "    .\run.ps1 excel AAPL       # build the Excel workbook"
Write-Host "    .\run.ps1 data AAPL JPM O  # standardized SEC financials"
