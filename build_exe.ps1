# Builds dist\Mailer.exe: a single file that runs the dashboard without installing Python.
# Your data (config.ini, recipients.csv, logs ...) is kept next to the .exe, not inside it.
# Usage:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .build-venv)) { python -m venv .build-venv }
& .build-venv\Scripts\python.exe -m pip install --quiet --upgrade pip pyinstaller

& .build-venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --console `
    --name Mailer `
    --add-data "ui;ui" `
    --add-data "config.example.ini;." `
    app.py

Write-Host "`nBuilt dist\Mailer.exe. Copy it to any folder and double-click it; it creates config.ini there on first run."
