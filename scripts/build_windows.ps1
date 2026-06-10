$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

if (-not (Test-Path ".venv")) {
  py -3.11 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt

$env:PLAYWRIGHT_BROWSERS_PATH = "0"
$env:PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT = "300000"
.\.venv\Scripts\python.exe -m playwright install chromium

$env:PYTHONPATH = "src"
$env:PYINSTALLER_CONFIG_DIR = ".pyinstaller-cache"
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
$env:PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT = "300000"
.\.venv\Scripts\pyinstaller.exe --noconfirm packaging\ai-rpa-desktop.spec

Write-Host ""
Write-Host "Build complete: dist\AI RPA Starter\AI RPA Starter.exe"
