<#
.SYNOPSIS
    Run the DCF model without worrying about PATH or activating the venv.

.DESCRIPTION
    Calls the virtual environment's interpreter directly, so this works whether
    or not `python` resolves on your PATH (on Windows it frequently does not —
    the Microsoft Store alias intercepts it).

.EXAMPLE
    .\run.ps1 app                 # Streamlit web app
    .\run.ps1 value AAPL          # DCF report in the terminal
    .\run.ps1 value AAPL --years 10
    .\run.ps1 excel AAPL          # build the Excel workbook
    .\run.ps1 data AAPL JPM O     # standardized SEC financials
    .\run.ps1 verify AAPL         # check the workbook recalculates correctly
    .\run.ps1 test                # headless app tests
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("app", "value", "excel", "data", "verify", "test")]
    [string]$Command = "app",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "No virtual environment found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

if (-not $env:SEC_UA_EMAIL) {
    Write-Host "SEC_UA_EMAIL is not set - the SEC will reject requests with HTTP 403." -ForegroundColor Yellow
    Write-Host 'Set it with:  $env:SEC_UA_EMAIL = "you@example.com"' -ForegroundColor Gray
    Write-Host ""
}

switch ($Command) {
    "app"    { & $venvPython -m streamlit run app.py }
    "value"  { & $venvPython valuation.py @Args }
    "excel"  { & $venvPython build_excel.py @Args }
    "data"   { & $venvPython sec_pull.py @Args }
    "verify" { & $venvPython verify_model.py @Args }
    "test"   { & $venvPython test_app.py }
}

if ($null -eq $LASTEXITCODE) { exit 0 }
exit $LASTEXITCODE
