# Creates a desktop shortcut that launches the DCF model.
# Invoked by "Create Desktop Shortcut.cmd" — you can also run it directly.

$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$launcher = Join-Path $projectDir "Launch DCF Model.cmd"

if (-not (Test-Path $launcher)) {
    Write-Host "Could not find 'Launch DCF Model.cmd' next to this script." -ForegroundColor Red
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "DCF Valuation Model.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = $projectDir
$shortcut.Description = "Dynamic DCF Valuation Model - runs locally on this computer"
# A chart-like icon from the Windows shell icon library.
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$shortcut.Save()

Write-Host ""
Write-Host "  Shortcut created on your desktop:" -ForegroundColor Green
Write-Host "    DCF Valuation Model"
Write-Host ""
Write-Host "  Double-click it any time to start the tool."
Write-Host ""
