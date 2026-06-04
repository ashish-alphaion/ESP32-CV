ESP32 Connection Dashboard — Release Package
=============================================

HOW TO INSTALL AND RUN
-----------------------
1. Right-click  INSTALL_AND_RUN.bat
2. Select       "Run as administrator"
3. The installer will:
     - Install Silicon Labs CP210x USB driver  (for CP2102 boards)
     - Install WCH CH340/CH341 USB driver      (for CH340 boards)
     - Launch ESP32Monitor.exe automatically

That is all. No Python installation required.


FOLDER STRUCTURE
----------------
ESP32_SOFTWARE_TEST\
  ESP32Monitor.exe                        <- Main application (double-click to run after install)
  INSTALL_AND_RUN.bat                     <- Run this FIRST (as Administrator)
  README.txt                              <- This file
  drivers\
    CP210x\
      CP210xVCPInstaller_x64.exe          <- Silicon Labs CP210x driver (64-bit)
    CH340\
      CH341SER.EXE                        <- WCH CH340/CH341 driver
  arduino-cli_1.5.0_Windows_64bit\
    arduino-cli.exe                       <- Required for OTA firmware update feature
  esp32_combined_v1\
    esp32_combined_v1.ino                 <- ESP32 firmware v1.0 sketch
  esp32_combined_v2\
    esp32_combined_v2.ino                 <- ESP32 firmware v2.0 sketch (used for OTA update)


SUPPORTED ESP32 USB CHIPS
--------------------------
  CP2102 / CP2104   Silicon Labs  (most common, blue boards)
  CH340 / CH341     WCH           (common on cheap clone boards)
  FT232             FTDI          (less common)


FEATURES
--------
  - Auto-detects ESP32 over USB or Bluetooth LE
  - USB always takes priority; BLE resumes when USB is unplugged
  - Detects firmware version over USB and BLE
  - Prompts to update firmware v1.0 to v2.0 over USB (OTA)
  - Serial monitor with send command, baud rate selector, save log
  - Shortcuts: Ctrl+M = toggle monitor   Ctrl+L = clear log


PREREQUISITES
-------------
  Windows 10 / 11 (64-bit)
  Bluetooth must be enabled for BLE features
  USB cable must be plugged in for USB features
