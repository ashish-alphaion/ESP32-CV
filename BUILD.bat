@echo off
title AlphaSense - Build System
color 1F
cls

echo.
echo  ================================================================
echo    AlphaSense  -  Full Build System  v11
echo    Steps:  Clean  ^>  Deps  ^>  EXE  ^>  Inno Setup  ^>  Installer
echo  ================================================================
echo.

:: Change to the directory of this bat file
cd /d "%~dp0"

set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
set "INNO_URL=https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe"
set "INNO_INSTALLER=%TEMP%\innosetup_installer.exe"
set "APP_NAME=AlphaSense"
set "APP_VERSION=1.0.0"

:: ══════════════════════════════════════════════════════════════════
:: STEP 1 — Clean previous build artefacts
:: ══════════════════════════════════════════════════════════════════
echo  [1/6] Cleaning previous build...
if exist "AlphaSense.exe"         del /f /q "AlphaSense.exe"
if exist "AlphaSense.spec"        del /f /q "AlphaSense.spec"
if exist "build"                  rmdir /s /q "build"
if exist "Output\AlphaSense_Setup.exe" del /f /q "Output\AlphaSense_Setup.exe"
echo        [OK] Clean done.
echo.

:: ══════════════════════════════════════════════════════════════════
:: STEP 2 — Check Python
:: ══════════════════════════════════════════════════════════════════
echo  [2/6] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found. Install Python 3.10+ and re-run.
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo        Found: %%v
echo.

:: ══════════════════════════════════════════════════════════════════
:: STEP 3 — Install / upgrade all Python dependencies
:: ══════════════════════════════════════════════════════════════════
echo  [3/6] Installing Python dependencies...
pip install --upgrade ^
    pyinstaller ^
    bleak ^
    pyserial ^
    pillow ^
    pystray ^
    --quiet --disable-pip-version-check
if %errorlevel% neq 0 (
    echo  [ERROR] pip install failed.
    pause & exit /b 1
)
echo        [OK] Dependencies ready.
echo.

:: ══════════════════════════════════════════════════════════════════
:: STEP 4 — Generate wizard BMP images from icon.png
:: ══════════════════════════════════════════════════════════════════
echo  [4/6] Building AlphaSense.exe with PyInstaller...
echo        Generating wizard images...
python -c "from PIL import Image; img=Image.open('icon.png').convert('RGB'); img.resize((55,58)).save('wizard_small.bmp','BMP'); img.resize((164,314)).save('wizard_large.bmp','BMP'); print('       BMP images OK')"

:: ── PyInstaller — bundle everything the app needs ─────────────────
::  --onefile        : single portable exe
::  --windowed       : no console window
::  --icon           : taskbar / file icon
::  --name           : output exe name
::  --hidden-import  : modules that PyInstaller misses by introspection
::  --add-data       : non-Python files copied into the bundle
::  --distpath .     : put AlphaSense.exe in the project root
::  --workpath build : temp build files go in ./build
::  --specpath .     : .spec file stays in project root
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "%APP_NAME%" ^
    --icon "icon_fixed.ico" ^
    --distpath "." ^
    --workpath "build" ^
    --specpath "." ^
    --hidden-import "bleak" ^
    --hidden-import "bleak.backends.winrt" ^
    --hidden-import "bleak.backends.winrt.scanner" ^
    --hidden-import "bleak.backends.winrt.client" ^
    --hidden-import "serial" ^
    --hidden-import "serial.tools" ^
    --hidden-import "serial.tools.list_ports" ^
    --hidden-import "pystray" ^
    --hidden-import "pystray._win32" ^
    --hidden-import "PIL" ^
    --hidden-import "PIL.Image" ^
    --hidden-import "PIL.ImageDraw" ^
    --hidden-import "winsound" ^
    --hidden-import "winreg" ^
    --hidden-import "asyncio" ^
    --hidden-import "json" ^
    --hidden-import "hashlib" ^
    --hidden-import "hmac" ^
    --hidden-import "zipfile" ^
    --hidden-import "urllib.request" ^
    "app.py"

if not exist "AlphaSense.exe" (
    echo.
    echo  [ERROR] PyInstaller failed — AlphaSense.exe not created.
    echo          Check the output above for details.
    pause & exit /b 1
)
echo        [OK] AlphaSense.exe built successfully.
echo.

:: ══════════════════════════════════════════════════════════════════
:: STEP 5 — Install Inno Setup 6 if not present
:: ══════════════════════════════════════════════════════════════════
echo  [5/6] Checking Inno Setup 6...
if exist "%ISCC%" (
    echo        [OK] Inno Setup 6 already installed.
    goto :compile
)

echo        Not found — trying winget...
winget install --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements >nul 2>&1
if exist "%ISCC%" (
    echo        [OK] Inno Setup 6 installed via winget.
    goto :compile
)

echo        winget failed — downloading installer directly...
powershell -Command "Invoke-WebRequest -Uri '%INNO_URL%' -OutFile '%INNO_INSTALLER%' -UseBasicParsing"
if not exist "%INNO_INSTALLER%" (
    echo  [ERROR] Could not download Inno Setup.
    echo          Install manually from: https://jrsoftware.org/isdl.php
    pause & exit /b 1
)
echo        Installing Inno Setup silently...
"%INNO_INSTALLER%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
timeout /t 10 /nobreak >nul
if not exist "%ISCC%" (
    echo  [ERROR] Inno Setup installation failed.
    pause & exit /b 1
)
echo        [OK] Inno Setup 6 installed.

:compile
echo.

:: ══════════════════════════════════════════════════════════════════
:: STEP 6 — Compile the installer with Inno Setup
:: ══════════════════════════════════════════════════════════════════
echo  [6/6] Compiling AlphaSense_Setup.exe installer...
if not exist "Output" mkdir "Output"

"%ISCC%" "AlphaSense.iss"

if not exist "Output\AlphaSense_Setup.exe" (
    echo.
    echo  [ERROR] Inno Setup compilation failed.
    echo          Check the output above for details.
    pause & exit /b 1
)

:: ══════════════════════════════════════════════════════════════════
:: DONE
:: ══════════════════════════════════════════════════════════════════
echo.
echo  ================================================================
echo    BUILD COMPLETE!
echo.
echo    Installer : %~dp0Output\AlphaSense_Setup.exe
echo.
echo    What the installer does on the customer's PC:
echo      - Installs AlphaSense.exe to Program Files\AlphaSense
echo      - Bundles AlphaSense_v1 and AlphaSense_v2 firmware sketches
echo      - Installs arduino-cli (needed for OTA firmware updates)
echo      - Silently installs CP210x USB driver (Silicon Labs)
echo      - Silently installs CH340/CH341 USB driver (WCH)
echo      - Creates Start Menu shortcut
echo      - Optional: Desktop shortcut
echo      - Optional: Launch app after install
echo  ================================================================
echo.
explorer "%~dp0Output"
pause
