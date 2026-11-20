@echo off
title Build RGB Studio
rem ============================================================
rem  Rebuilds RGB Studio after code changes (about a minute),
rem  then starts it. The app is:  dist\win-unpacked\RGB Studio.exe
rem ============================================================
net session >nul 2>&1
if errorlevel 1 (
    echo Asking for administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"

if not exist "node_modules\electron-builder" (
    echo First run: installing build tools...
    call npm install
    if errorlevel 1 goto failed
)
if not exist "bin\OpenRGB\OpenRGB.exe" (
    echo OpenRGB backend missing - downloading it...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\update-openrgb.ps1"
    if errorlevel 1 goto failed
)

echo [1/3] Stopping RGB Studio...
taskkill /f /t /im "RGB Studio.exe" >nul 2>&1
taskkill /f /im "OpenRGB.exe" >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/3] Building...
call npm run build
if errorlevel 1 goto failed
if not exist "dist\win-unpacked\RGB Studio.exe" goto failed

echo [3/3] Starting RGB Studio...
rem Don't tie the app to this window (closing it would close the app).
set ELECTRON_NO_ATTACH_CONSOLE=1
start "" "dist\win-unpacked\RGB Studio.exe"
echo.
echo Done. This window closes by itself.
timeout /t 5 >nul
exit /b 0

:failed
echo.
echo *** BUILD FAILED - send this window to Claude. ***
echo     Nothing else was changed.
pause
exit /b 1
