AlphaSense Dashboard
=====================

HOW TO RUN
----------
Double-click  AlphaSense.exe

No Python installation required.


FOLDER STRUCTURE (do not move or rename these)
-----------------------------------------------
AlphaSense\
  AlphaSense.exe                          <- the application
  arduino-cli_1.5.0_Windows_64bit\
    arduino-cli.exe                       <- required for OTA firmware update
  AlphaSense_v2\
    AlphaSense_v2.ino                     <- firmware v2.0 sketch (for OTA update)
  AlphaSense_v1\
    AlphaSense_v1.ino                     <- firmware v1.0 sketch


PREREQUISITES ON THE TARGET PC
-------------------------------
1. Silicon Labs CP210x USB driver  (installed automatically by the setup wizard)
   Required so Windows recognises the USB port.

2. WCH CH340/CH341 USB driver  (installed automatically by the setup wizard)

3. Windows 10 / 11  (64-bit)
   Bluetooth must be enabled for BLE features.


PRODUCT ACTIVATION
------------------
On first launch you will be asked to enter the product key printed on your
AlphaSense device label.  Format:  AS-XXXX-XXXX-XXXX-XXXX
The key is saved to this machine and is only required once.


FEATURES
--------
- Product key activation — one-time entry, machine-bound license
- HMAC-SHA256 device authentication — only your device can connect
- Auto-detects AlphaSense over USB or Bluetooth LE
- USB always takes priority; BLE resumes when USB is unplugged
- Detects firmware version — auto-downloads and installs latest update
- 4-phase OTA update: Check → Download → Compile → Upload
- Device Status panel — connection type, port, firmware, auth status
- System tray icon with colour-coded connection status
- Sound alerts on connect, disconnect, auth failure, update complete


FIRMWARE TO FLASH ON THE DEVICE
--------------------------------
Use AlphaSense_v1, AlphaSense_v2, AlphaSense_bluetooth, or AlphaSense_usb
sketches (in the project folder) via Arduino IDE before first use.
The DEVICE_SECRET in the sketch must match the product key you assign to that unit.
