@echo off
title ESP32 Connection Dashboard ? Installer
color 1F
cls

echo.
echo  ============================================================
echo    ESP32 Connection Dashboard ? Setup ^& Installer
echo    Installs all required USB drivers then launches the app
echo  ============================================================
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  [!] This installer requires Administrator privileges.
    echo      Right-click INSTALL_AND_RUN.bat and choose "Run as administrator"
    echo.
    pause
    exit /b 1
)

echo  [OK] Running as Administrator
echo.

set "BASEDIR=%~dp0"

echo  [1/3] Installing Silicon Labs CP210x USB Driver...
if exist "%BASEDIR%drivers\CP210x\CP210xVCPInstaller_x64.exe" (
    "%BASEDIR%drivers\CP210x\CP210xVCPInstaller_x64.exe" /S
    echo       [OK] CP210x driver installed.
) else (
    echo       [SKIP] CP210x installer not found.
)
echo.

echo  [2/3] Installing WCH CH340/CH341 USB Driver...
if exist "%BASEDIR%drivers\CH340\CH341SER.EXE" (
    "%BASEDIR%drivers\CH340\CH341SER.EXE" /S
    echo       [OK] CH340 driver installed.
) else (
    echo       [SKIP] CH340 installer not found.
)
echo.

echo  [3/3] Checking for ESP32Monitor.exe...
if not exist "%BASEDIR%ESP32Monitor.exe" (
    echo  [ERROR] ESP32Monitor.exe not found in %BASEDIR%
    pause
    exit /b 1
)
echo       [OK] ESP32Monitor.exe found.
echo.

echo  ============================================================
echo    All drivers installed. Launching ESP32 Monitor...
echo  ============================================================
echo.

timeout /t 2 /nobreak >nul
start "" "%BASEDIR%ESP32Monitor.exe"
exit /b 0
