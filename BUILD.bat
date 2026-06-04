@echo off
title AlphaSense - Build System
color 1F
cls

echo.
echo  ================================================================
echo    AlphaSense  -  Full Build System
echo    Builds EXE  +  Compiles Installer  in one click
echo  ================================================================
echo.

:: Change to the directory containing this bat file
cd /d "%~dp0"

set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
set "INNO_URL=https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe"
set "INNO_INSTALLER=%TEMP%\innosetup_installer.exe"

:: ?? STEP 1: Check Python ?????????????????????????????????????????????????
echo  [1/5] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found. Please install Python 3.10+ and re-run.
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo        Found: %%v
echo.

:: ?? STEP 2: Install Python dependencies ??????????????????????????????????
echo  [2/5] Installing Python dependencies...
pip install pyinstaller bleak pyserial pillow --quiet --disable-pip-version-check
echo        [OK] Dependencies ready.
echo.

:: ?? STEP 3: Build AlphaSense.exe with PyInstaller ????????????????????????
echo  [3/5] Building AlphaSense.exe...
if exist "AlphaSense.exe" del /f /q "AlphaSense.exe"

pyinstaller --onefile --windowed --name "AlphaSense" --icon "icon_fixed.ico" --distpath "." --workpath "build" --specpath "." "app.py"

if not exist "AlphaSense.exe" (
    echo  [ERROR] PyInstaller failed - AlphaSense.exe not created.
    pause & exit /b 1
)
echo        [OK] AlphaSense.exe built successfully.
echo.

:: ?? STEP 4: Install Inno Setup if not present ????????????????????????????
echo  [4/5] Checking Inno Setup 6...
if exist "%ISCC%" (
    echo        [OK] Inno Setup 6 already installed.
    goto compile
)

:: Try winget first (fastest, no download needed)
echo        Trying winget install...
winget install --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements >nul 2>&1
if exist "%ISCC%" (
    echo        [OK] Inno Setup installed via winget.
    goto compile
)

:: Fallback: direct download
echo        Downloading Inno Setup installer...
powershell -Command "Invoke-WebRequest -Uri '%INNO_URL%' -OutFile '%INNO_INSTALLER%' -UseBasicParsing"
if not exist "%INNO_INSTALLER%" (
    echo  [ERROR] Failed to download Inno Setup.
    echo          Please install manually from: https://jrsoftware.org/isdl.php
    pause & exit /b 1
)
echo        Installing Inno Setup silently...
"%INNO_INSTALLER%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
timeout /t 8 /nobreak >nul
if not exist "%ISCC%" (
    echo  [ERROR] Inno Setup installation failed.
    pause & exit /b 1
)
echo        [OK] Inno Setup 6 installed.

:compile
echo.

:: ?? STEP 5: Compile the installer ????????????????????????????????????????
echo  [5/5] Compiling AlphaSense_Setup.exe installer...
if not exist "Output" mkdir "Output"

:: Generate wizard BMP images from icon.png (required by Inno Setup)
python -c "from PIL import Image; img=Image.open('icon.png').convert('RGB'); img.resize((55,58)).save('wizard_small.bmp','BMP'); img.resize((164,314)).save('wizard_large.bmp','BMP')"

"%ISCC%" "AlphaSense.iss"

if not exist "Output\AlphaSense_Setup.exe" (
    echo  [ERROR] Inno Setup compilation failed.
    pause & exit /b 1
)

echo.
echo  ================================================================
echo    BUILD COMPLETE!
echo.
echo    Installer: %~dp0Output\AlphaSense_Setup.exe
echo.
echo    Distribute this single file - it installs everything:
echo      - AlphaSense app  to  C:\Program Files\AlphaSense
echo      - Silicon Labs CP210x USB driver  (silent)
echo      - WCH CH340/CH341 USB driver      (silent)
echo      - Start Menu shortcut
echo      - Optional Desktop shortcut
echo  ================================================================
echo.
explorer "%~dp0Output"
pause
