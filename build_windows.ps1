# Build Imbabot.exe on Windows.
# Usage (PowerShell, from the project folder):
#   ./build_windows.ps1
#
# Produces dist\Imbabot.exe (a single, double-clickable file).

$ErrorActionPreference = "Stop"

Write-Host "==> Creating virtual environment (.venv)..."
if (-Not (Test-Path ".venv")) { python -m venv .venv }

Write-Host "==> Installing dependencies..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller

Write-Host "==> Running self-test (offline)..."
& .\.venv\Scripts\python.exe -m imbabot.cli selftest

Write-Host "==> Building Imbabot.exe..."
& .\.venv\Scripts\pyinstaller.exe imbabot.spec --noconfirm

Write-Host ""
Write-Host "Done. Your executable is at: dist\Imbabot.exe" -ForegroundColor Green
