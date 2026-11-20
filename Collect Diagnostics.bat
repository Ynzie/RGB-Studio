@echo off
title RGB Studio - Collect Diagnostics
rem Saves a report + logs into the "diagnostics" folder for Claude to read.
rem Changes nothing on your PC.
net session >nul 2>&1
if errorlevel 1 (
    echo Asking for administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\diagnose.ps1"
echo.
echo Done. Tell Claude "diagnostics collected".
pause
