@echo off
title Build RGB Studio Installer
rem ============================================================
rem  Makes dist\RGB-Studio-Setup.exe - copy it to another PC
rem  (e.g. your wife's) and run it. Only needed for other PCs.
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

echo [1/2] Stopping RGB Studio...
taskkill /f /t /im "RGB Studio.exe" >nul 2>&1
taskkill /f /im "OpenRGB.exe" >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/2] Building the installer (a few minutes - it compresses everything)...
call npm run installer
if errorlevel 1 goto failed
if not exist "dist\RGB-Studio-Setup.exe" goto failed

rem Don't tie the app to this window (closing it would close the app).
set ELECTRON_NO_ATTACH_CONSOLE=1
start "" "dist\win-unpacked\RGB Studio.exe"
explorer /select,"%~dp0dist\RGB-Studio-Setup.exe"
echo.
echo Done: dist\RGB-Studio-Setup.exe
echo On the other PC: run it, click Yes on the admin prompt once, and
echo install PawnIO (https://pawnio.eu) if the app asks for it.
pause
exit /b 0

:failed
echo.
echo *** BUILD FAILED - send this window to Claude. ***
pause
exit /b 1
