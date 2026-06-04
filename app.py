"""
AlphaSense  ·  v11
──────────────────────────────────────────────────────────────────────────────
Theme   : White / Royal Blue / Matte Grey
License : One-time product key entry on first launch.
          Key is saved encrypted to %APPDATA%\AlphaSense\license.dat
          and bound to the machine so it cannot be copied to another PC.
Auth    : HMAC-SHA256 challenge-response using the saved product key.
          Device must prove it holds the same key → otherwise Unauthorized.
Security:
  • Activation lockout after 5 wrong keys (30 min registry-backed cooldown)
  • Per-connection auth nonce — no USB/BLE race condition
  • XOR-obfuscated firmware secret — not plain text in flash
  • Zip-slip protection on OTA extraction
  • 50 MB download cap on firmware updates
  • Thread-safe LATEST_VERSION via lock
Rules   :
  • First launch  → product key dialog (unclosable)
  • Subsequent    → key loaded from license.dat, dialog skipped
  • Device auth   → challenge/response with saved key
  • Wrong device  → non-closable Unauthorized screen
  • USB priority over BLE
──────────────────────────────────────────────────────────────────────────────
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import asyncio
import serial
import serial.tools.list_ports
from bleak import BleakScanner, BleakClient
import time
import subprocess
import os
import sys
import ctypes
import hmac as _hmac_mod
import hashlib
import secrets
import base64
import json
import re
import urllib.request
import urllib.error
import zipfile
import tempfile
import shutil

# Thread-safe wrapper for LATEST_VERSION (fix #14)
_version_lock   = threading.Lock()
_LATEST_VERSION = "2.0"

def get_latest_version() -> str:
    with _version_lock:
        return _LATEST_VERSION

def set_latest_version(v: str):
    with _version_lock:
        global _LATEST_VERSION
        _LATEST_VERSION = v

# pystray + Pillow for system tray
try:
    import pystray
    from PIL import Image, ImageDraw
    _TRAY_AVAILABLE = True
except ImportError:
    _TRAY_AVAILABLE = False

# ── Windows DPI awareness ─────────────────────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR     = (os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)
                else os.path.dirname(os.path.abspath(__file__)))
ARDUINO_CLI  = os.path.join(BASE_DIR, "arduino-cli_1.5.0_Windows_64bit", "arduino-cli.exe")
SKETCH_V2DIR = os.path.join(BASE_DIR, "AlphaSense_v2")
FQBN         = "esp32:esp32:esp32"

# License file lives in %APPDATA%\AlphaSense\ so it survives app reinstalls
_APPDATA     = os.environ.get("APPDATA", BASE_DIR)
_LICENSE_DIR = os.path.join(_APPDATA, "AlphaSense")
LICENSE_FILE = os.path.join(_LICENSE_DIR, "license.dat")

# ─────────────────────────────────────────────────────────────────────────────
# DEVICE / CONNECTION CONFIG
# ─────────────────────────────────────────────────────────────────────────────
USB_BAUD_RATE   = 115200
USB_POLL_SEC    = 2
BLE_POLL_SEC    = 6
ESP32_USB_HINTS = ["CP210", "CH340", "CH341", "FTDI", "USB Serial", "USB-SERIAL",
                   "Silicon Labs", "wch.cn", "1a86", "10c4", "0403"]
NUS_TX_UUID     = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_RX_UUID     = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
AUTH_TIMEOUT    = 8   # seconds

# Activation lockout (fix #8) — stored in registry
MAX_FAILED_ACTIVATIONS = 5
LOCKOUT_DURATION_SEC   = 1800   # 30 minutes

# ─────────────────────────────────────────────────────────────────────────────
# OTA — ONLINE FIRMWARE UPDATE CONFIG
# ─────────────────────────────────────────────────────────────────────────────
# Point OTA_MANIFEST_URL at a JSON file you host (GitHub Releases recommended).
#
# Manifest format:
# {
#   "latest_version": "2.1",
#   "changelog":      "Fixed BLE reconnect bug, improved auth speed.",
#   "firmware_url":   "https://github.com/YOU/alphasense/releases/download/v2.1/esp32_v2.zip",
#   "firmware_sha256":"abc123..."
# }
#
# Set to None to always use the local sketch folder (offline / development mode).
OTA_MANIFEST_URL = None
# e.g. "https://raw.githubusercontent.com/yourname/alphasense-firmware/main/manifest.json"

OTA_TEMP_DIR = os.path.join(_APPDATA, "AlphaSense", "ota_temp")

# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT KEY FORMAT
# Keys look like:  AS-XXXX-XXXX-XXXX-XXXX
# where X is alphanumeric (uppercase).  You generate these and flash one
# matching key into each device unit's DEVICE_SECRET in the firmware.
# ─────────────────────────────────────────────────────────────────────────────
KEY_PATTERN = re.compile(
    r'^AS-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$'
)

def is_valid_key_format(key: str) -> bool:
    return bool(KEY_PATTERN.match(key.upper().strip()))

# ─────────────────────────────────────────────────────────────────────────────
# LICENSE  — encrypt/decrypt & machine binding
# ─────────────────────────────────────────────────────────────────────────────
def _get_machine_id() -> str:
    """
    Return a stable machine identifier.
    Uses Windows MachineGuid + CPU info for stronger binding (fix #12).
    Falls back to hostname hash if registry is unavailable.
    """
    parts = []
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Cryptography")
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        parts.append(guid)
    except Exception:
        pass
    # Add processor identifier for extra binding strength
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        cpu, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        winreg.CloseKey(key)
        parts.append(cpu.strip())
    except Exception:
        pass
    if parts:
        return hashlib.sha256("|".join(parts).encode()).hexdigest()
    # Final fallback — hostname (weaker but better than nothing)
    import socket
    return hashlib.sha256(
        f"fallback-{socket.gethostname()}".encode()
    ).hexdigest()

def _derive_file_key(machine_id: str) -> bytes:
    """Derive a 32-byte key from the machine ID for encrypting license.dat."""
    return hashlib.sha256(
        f"AlphaSense-License-{machine_id}".encode("utf-8")
    ).digest()

def _xor_encrypt(data: bytes, key: bytes) -> bytes:
    """Simple XOR stream cipher using key repeated to cover data length."""
    key_len = len(key)
    return bytes(data[i] ^ key[i % key_len] for i in range(len(data)))

def save_license(product_key: str) -> None:
    """Encrypt and persist the product key to license.dat."""
    os.makedirs(_LICENSE_DIR, exist_ok=True)
    machine_id = _get_machine_id()
    file_key   = _derive_file_key(machine_id)

    payload = json.dumps({
        "key":        product_key.upper().strip(),
        "machine_id": machine_id,
        "ts":         int(time.time()),
    }).encode("utf-8")

    encrypted = _xor_encrypt(payload, file_key)
    # Prepend HMAC so we can detect tampering
    mac = _hmac_mod.new(file_key, encrypted, hashlib.sha256).hexdigest()
    blob = json.dumps({
        "data": base64.b64encode(encrypted).decode(),
        "mac":  mac,
    })
    with open(LICENSE_FILE, "w", encoding="utf-8") as f:
        f.write(blob)

def load_license() -> str | None:
    """
    Load and verify license.dat.
    Returns the product key string, or None if missing / tampered / wrong machine.
    """
    if not os.path.isfile(LICENSE_FILE):
        return None
    try:
        machine_id = _get_machine_id()
        file_key   = _derive_file_key(machine_id)

        with open(LICENSE_FILE, "r", encoding="utf-8") as f:
            blob = json.load(f)

        encrypted = base64.b64decode(blob["data"])
        stored_mac = blob["mac"]

        # Verify HMAC first (tamper check)
        expected_mac = _hmac_mod.new(file_key, encrypted, hashlib.sha256).hexdigest()
        if not _hmac_mod.compare_digest(stored_mac, expected_mac):
            return None   # file was tampered

        decrypted = _xor_encrypt(encrypted, file_key)
        payload   = json.loads(decrypted.decode("utf-8"))

        # Machine binding check
        if payload.get("machine_id") != machine_id:
            return None   # copied from another machine

        key = payload.get("key", "")
        return key if is_valid_key_format(key) else None
    except Exception:
        return None

def delete_license() -> None:
    """Remove the license file (for testing / reset)."""
    try:
        os.remove(LICENSE_FILE)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# ACTIVATION LOCKOUT  — fix #8
# Tracks failed activation attempts in the Windows registry.
# After MAX_FAILED_ACTIVATIONS wrong keys → locked for LOCKOUT_DURATION_SEC.
# ─────────────────────────────────────────────────────────────────────────────
_LOCKOUT_REG_PATH = r"SOFTWARE\AlphaSense\Activation"

def _reg_get(name: str, default):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _LOCKOUT_REG_PATH)
        val, _ = winreg.QueryValueEx(key, name)
        winreg.CloseKey(key)
        return val
    except Exception:
        return default

def _reg_set(name: str, value):
    try:
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, _LOCKOUT_REG_PATH)
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
        winreg.CloseKey(key)
    except Exception:
        pass

def activation_is_locked() -> tuple[bool, int]:
    """Return (locked: bool, seconds_remaining: int)."""
    locked_until = int(_reg_get("LockedUntil", "0"))
    now = int(time.time())
    if locked_until > now:
        return True, locked_until - now
    return False, 0

def activation_record_failure():
    """Increment failure counter; lock if threshold reached."""
    failures = int(_reg_get("FailCount", "0")) + 1
    _reg_set("FailCount", failures)
    if failures >= MAX_FAILED_ACTIVATIONS:
        lock_until = int(time.time()) + LOCKOUT_DURATION_SEC
        _reg_set("LockedUntil", lock_until)
        _reg_set("FailCount", "0")   # reset counter after lockout starts

def activation_record_success():
    """Clear failure counter on successful activation."""
    _reg_set("FailCount", "0")
    _reg_set("LockedUntil", "0")

# ─────────────────────────────────────────────────────────────────────────────
# HMAC AUTH  — using the saved product key
# ─────────────────────────────────────────────────────────────────────────────
def compute_expected_response(nonce: str, product_key: str) -> str:
    """HMAC-SHA256(product_key, nonce) — same algorithm as firmware."""
    return _hmac_mod.new(
        product_key.encode("utf-8"),
        nonce.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def verify_device_response(nonce: str, device_response: str,
                            product_key: str) -> bool:
    """Return True if the device's HMAC matches our saved product key."""
    expected = compute_expected_response(nonce, product_key)
    return _hmac_mod.compare_digest(expected.lower(), device_response.lower())

# ─────────────────────────────────────────────────────────────────────────────
# OTA HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def ota_fetch_manifest() -> dict | None:
    """
    Download and parse the firmware manifest JSON.
    Returns dict on success, None if offline or URL not configured.
    """
    if not OTA_MANIFEST_URL:
        return None
    try:
        req = urllib.request.Request(
            OTA_MANIFEST_URL,
            headers={"User-Agent": "AlphaSense-OTA/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Validate required fields
        if "latest_version" in data and "firmware_url" in data:
            return data
    except Exception:
        pass
    return None


def ota_version_is_newer(remote: str, current: str) -> bool:
    """Return True if remote version is strictly newer than current."""
    def _parse(v):
        try:
            return tuple(int(x) for x in str(v).strip().split("."))
        except Exception:
            return (0,)
    return _parse(remote) > _parse(current)


def ota_download_firmware(url: str, progress_cb) -> str | None:
    """
    Download firmware zip to OTA_TEMP_DIR.
    progress_cb(pct: int, label: str) called periodically.
    Returns path to downloaded zip, or None on failure.
    Max download size: 50 MB (fix #13).
    """
    OTA_MAX_BYTES = 50 * 1024 * 1024   # 50 MB hard limit
    os.makedirs(OTA_TEMP_DIR, exist_ok=True)
    dest = os.path.join(OTA_TEMP_DIR, "firmware_latest.zip")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "AlphaSense-OTA/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            # Reject if Content-Length already exceeds limit
            if total > OTA_MAX_BYTES:
                return None
            downloaded = 0
            chunk = 8192
            with open(dest, "wb") as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    downloaded += len(buf)
                    if downloaded > OTA_MAX_BYTES:
                        return None   # abort — server lied about size
                    f.write(buf)
                    if total > 0:
                        pct = int(downloaded / total * 100)
                        kb = downloaded // 1024
                        total_kb = total // 1024
                        progress_cb(pct, f"Downloading…  {kb} / {total_kb} KB")
                    else:
                        kb = downloaded // 1024
                        progress_cb(-1, f"Downloading…  {kb} KB received")
        return dest
    except Exception:
        return None


def ota_verify_sha256(zip_path: str, expected_hex: str) -> bool:
    """Verify SHA-256 of downloaded file. Returns True if matches or no hash given."""
    if not expected_hex:
        return True
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower() == expected_hex.lower()


def ota_extract_firmware(zip_path: str) -> str | None:
    """
    Extract firmware zip into OTA_TEMP_DIR/extracted/.
    Zip-slip protection: rejects any entry that would escape the extract dir (fix #4).
    Returns path to the sketch folder (.ino file), or None on failure.
    """
    extract_dir = os.path.realpath(os.path.join(OTA_TEMP_DIR, "extracted"))
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Zip-slip check: validate every member path before extracting
            for member in zf.namelist():
                member_path = os.path.realpath(
                    os.path.join(extract_dir, member))
                if not member_path.startswith(extract_dir + os.sep) and \
                   member_path != extract_dir:
                    raise ValueError(
                        f"Zip-slip detected: {member!r} escapes extract dir")
            zf.extractall(extract_dir)
        # Find the .ino file and return its parent folder
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                if f.endswith(".ino"):
                    return root
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SOUND ALERTS  — uses Windows built-in system sounds, zero extra libraries
# ─────────────────────────────────────────────────────────────────────────────
import winsound

def _play_sound(sound_type: str):
    """
    Play a non-blocking Windows system sound in a daemon thread.

    sound_type values:
      "connect"    — device verified and connected  (pleasant chime)
      "disconnect" — device disconnected            (soft alert)
      "auth_fail"  — unauthorized device            (error tone)
      "upload_ok"  — firmware upload success        (success fanfare)
      "upload_fail"— firmware upload failed         (error tone)
    """
    _MAP = {
        "connect":     (winsound.MB_OK,                  0),   # default beep
        "disconnect":  (winsound.SND_ALIAS, "SystemExclamation"),
        "auth_fail":   (winsound.SND_ALIAS, "SystemHand"),
        "upload_ok":   (winsound.SND_ALIAS, "SystemAsterisk"),
        "upload_fail": (winsound.SND_ALIAS, "SystemHand"),
    }

    def _play():
        try:
            entry = _MAP.get(sound_type)
            if entry is None:
                return
            flags, arg = entry
            if flags == winsound.MB_OK:
                winsound.MessageBeep(winsound.MB_OK)
            else:
                winsound.PlaySound(arg, flags | winsound.SND_ASYNC)
        except Exception:
            pass   # sound failure must never affect app behaviour

    threading.Thread(target=_play, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# TRAY ICON HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_TRAY_SIZE = 64   # icon canvas size in pixels

def _make_tray_image(color: str) -> "Image.Image":
    """
    Draw a circular icon in the given hex colour on a transparent background.
    Used for the system tray icon — colour communicates device status at a glance.
    """
    img  = Image.new("RGBA", (_TRAY_SIZE, _TRAY_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Parse hex colour
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    margin = 6
    draw.ellipse(
        [margin, margin, _TRAY_SIZE - margin, _TRAY_SIZE - margin],
        fill=(r, g, b, 255),
        outline=(255, 255, 255, 180),
        width=3,
    )
    # Small "A" letter in white inside the circle
    draw.text((_TRAY_SIZE // 2 - 8, _TRAY_SIZE // 2 - 10), "A",
              fill=(255, 255, 255, 230),
              font=None)   # default PIL font — fine for small tray icon
    return img


# Tray status colours
_TRAY_COLOR = {
    "idle":          "#8A9AB5",   # grey   — no device
    "connecting":    "#C97A00",   # amber  — connecting / authenticating
    "connected":     "#1A8F5A",   # green  — verified and active
    "unauthorized":  "#C0392B",   # red    — auth failed
    "uploading":     "#2B4FD9",   # blue   — firmware update in progress
}


# ─────────────────────────────────────────────────────────────────────────────
# COLOUR PALETTE
# ─────────────────────────────────────────────────────────────────────────────
WHITE       = "#FFFFFF"
OFF_WHITE   = "#F4F6FA"
CARD_BG     = "#FFFFFF"
CARD_SH     = "#E4E9F2"
MATTE_DARK  = "#2C3A50"
MATTE_MID   = "#5A6A80"
MATTE_LITE  = "#8A9AB5"
GREY_LINE   = "#D0D8E8"
GREY_BG2    = "#EAEEf5"
ROYAL       = "#2B4FD9"
ROYAL_DARK  = "#1A3AB8"
ROYAL_LITE  = "#5B7FFF"
ROYAL_PALE  = "#E8EDFF"
ROYAL_XPALE = "#F0F3FF"
SUCCESS     = "#1A8F5A"
SUCCESS_BG  = "#E6F5EE"
WARN        = "#C97A00"
WARN_BG     = "#FFF7E0"
DANGER      = "#C0392B"
DANGER_BG   = "#FDECEA"

# ─────────────────────────────────────────────────────────────────────────────
# FONTS
# ─────────────────────────────────────────────────────────────────────────────
F_TITLE   = ("Segoe UI", 18, "bold")
F_H1      = ("Segoe UI", 13, "bold")
F_H2      = ("Segoe UI", 11, "bold")
F_BODY    = ("Segoe UI", 10, "bold")
F_BODY_N  = ("Segoe UI", 10)
F_SMALL   = ("Segoe UI",  9, "bold")
F_SMALL_N = ("Segoe UI",  9)
F_MONO    = ("Consolas", 10)
F_MONO_B  = ("Consolas", 10, "bold")
F_STATUS  = ("Segoe UI",  8, "bold")
F_CARD_VAL= ("Segoe UI", 22, "bold")
F_CARD_LBL= ("Segoe UI",  9, "bold")


# ─────────────────────────────────────────────────────────────────────────────
# ProductKeyDialog  — shown on first launch, cannot be closed or skipped
# ─────────────────────────────────────────────────────────────────────────────
class ProductKeyDialog(tk.Toplevel):
    """
    Full-window modal asking for the product key.
    Single input field — auto-formats to AS-XXXX-XXXX-XXXX-XXXX as you type.
    Supports paste of raw key or formatted key.
    Cannot be closed or skipped.  Sets self.result on success.
    """

    # Max raw alphanumeric chars after the fixed "AS" prefix  (4 segments × 4)
    _MAX_RAW = 16
    # Positions in the display string where dashes sit: AS-XXXX-XXXX-XXXX-XXXX
    #   index:  0123456789...
    #           AS-XXXX-XXXX-XXXX-XXXX
    _DASH_POS = {2, 7, 12, 17}   # positions of '-' chars in the full 22-char key

    def __init__(self, parent):
        super().__init__(parent)
        self.title("AlphaSense — Product Activation")
        self.configure(bg=WHITE)
        self.resizable(False, False)
        self.grab_set()
        self.focus_force()
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.attributes("-topmost", True)
        self.result = None

        w, h = 580, 500
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w)//2}+{(sh - h)//2}")

        # ── Top stripe ──
        tk.Frame(self, bg=ROYAL, height=6).pack(fill="x")

        # ── Hero area ──
        hero = tk.Frame(self, bg=ROYAL_XPALE, pady=26)
        hero.pack(fill="x")
        tk.Label(hero, text="🔑",
                 font=("Segoe UI", 38), bg=ROYAL_XPALE).pack()
        tk.Label(hero, text="Product Activation",
                 font=("Segoe UI", 18, "bold"),
                 bg=ROYAL_XPALE, fg=MATTE_DARK).pack(pady=(8, 0))
        tk.Label(hero,
                 text="Enter the product key printed on your AlphaSense device.",
                 font=F_BODY_N, bg=ROYAL_XPALE, fg=MATTE_MID).pack(pady=(4, 0))

        tk.Frame(self, bg=GREY_LINE, height=1).pack(fill="x")

        # ── Body ──
        body = tk.Frame(self, bg=WHITE, padx=52, pady=30)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="PRODUCT KEY",
                 font=("Segoe UI", 8, "bold"),
                 bg=WHITE, fg=MATTE_LITE).pack(anchor="w")

        # ── Single formatted input field ──────────────────────────────────
        # Outer frame gives us a colored border we can change on validation
        self._entry_border = tk.Frame(body, bg=GREY_LINE, padx=1, pady=1)
        self._entry_border.pack(fill="x", pady=(6, 4))

        inner_bg = tk.Frame(self._entry_border, bg=GREY_BG2)
        inner_bg.pack(fill="x")

        self._key_var = tk.StringVar()
        self._entry = tk.Entry(
            inner_bg,
            textvariable=self._key_var,
            font=("Consolas", 15, "bold"),
            bg=GREY_BG2,
            fg=MATTE_DARK,
            insertbackground=ROYAL,
            relief="flat",
            bd=0,
            justify="center",
        )
        self._entry.pack(fill="x", ipady=14, padx=12)
        self._entry.focus_set()

        # Placeholder text
        self._entry.insert(0, "AS-XXXX-XXXX-XXXX-XXXX")
        self._entry.config(fg=MATTE_LITE)
        self._entry.bind("<FocusIn>",  self._on_focus_in)
        self._entry.bind("<FocusOut>", self._on_focus_out)

        # Intercept ALL key input so we can auto-format
        self._entry.bind("<KeyPress>", self._on_key_press)
        # Block the default key handling after our handler runs
        self._entry.bind("<Key>",      lambda e: "break")

        # Allow Ctrl+V paste
        self._entry.bind("<Control-v>", self._on_paste)
        self._entry.bind("<Control-V>", self._on_paste)

        self._key_var.trace_add("write", lambda *_: self._on_text_changed())

        # ── Progress dots ── (fill as segments complete)
        dots_frame = tk.Frame(body, bg=WHITE)
        dots_frame.pack(anchor="w", pady=(6, 0))
        self._seg_dots = []
        for i in range(4):
            lbl = tk.Label(dots_frame, text="○",
                           font=("Segoe UI", 10), bg=WHITE, fg=MATTE_LITE)
            lbl.pack(side="left", padx=(0, 6))
            self._seg_dots.append(lbl)

        # ── Hint ──
        tk.Label(body,
                 text="Format:  AS-XXXX-XXXX-XXXX-XXXX   ·   use letters A–Z and digits 0–9",
                 font=("Segoe UI", 8), bg=WHITE, fg=MATTE_LITE).pack(anchor="w", pady=(4, 0))

        # ── Char count indicator ──
        self._count_lbl = tk.Label(body, text="0 / 16 characters",
                                   font=("Segoe UI", 8), bg=WHITE, fg=MATTE_LITE)
        self._count_lbl.pack(anchor="e", pady=(2, 12))

        # ── Error label ──
        self._error_lbl = tk.Label(body, text="",
                                   font=("Segoe UI", 9, "bold"),
                                   bg=WHITE, fg=DANGER)
        self._error_lbl.pack(anchor="w", pady=(0, 10))

        # ── Activate button ──
        self._act_btn = tk.Button(
            body,
            text="  Activate  ",
            font=("Segoe UI", 12, "bold"),
            bg=ROYAL, fg=WHITE,
            activebackground=ROYAL_DARK,
            relief="flat", bd=0,
            padx=32, pady=13,
            cursor="hand2",
            command=self._on_activate,
        )
        self._act_btn.pack(pady=(0, 0))

        # ── Footer ──
        tk.Frame(self, bg=GREY_LINE, height=1).pack(fill="x")
        tk.Label(self,
                 text="This key is tied to this machine and cannot be transferred to another PC.",
                 font=("Segoe UI", 8), bg=CARD_SH, fg=MATTE_LITE,
                 pady=9).pack(fill="x")

        self.bind("<Return>", lambda e: self._on_activate())

        # Internal state — raw 16-char string (no dashes, no prefix)
        self._raw = ""
        self._placeholder_active = True

        # Check lockout immediately on open
        locked, secs = activation_is_locked()
        if locked:
            mins = secs // 60
            self._act_btn.config(state="disabled")
            self._error_lbl.config(
                text=f"⚠  Too many failed attempts.  Try again in {mins} min.",
                fg=DANGER)
            self._entry_border.config(bg=DANGER)

    # ── Placeholder handling ──────────────────────────────────────────────
    def _on_focus_in(self, event=None):
        if self._placeholder_active:
            self._entry.delete(0, "end")
            self._entry.config(fg=MATTE_DARK)
            self._placeholder_active = False

    def _on_focus_out(self, event=None):
        if not self._raw:
            self._entry.delete(0, "end")
            self._entry.insert(0, "AS-XXXX-XXXX-XXXX-XXXX")
            self._entry.config(fg=MATTE_LITE)
            self._placeholder_active = True

    # ── Format raw chars into display string ─────────────────────────────
    def _format_display(self, raw: str) -> str:
        """Build  AS-XXXX-XXXX-XXXX-XXXX  from up-to-16 raw alphanum chars."""
        segs = [raw[i:i+4] for i in range(0, len(raw), 4)]
        return "AS-" + "-".join(segs) if segs else "AS-"

    # ── Key press handler ─────────────────────────────────────────────────
    def _on_key_press(self, event):
        keysym = event.keysym

        if keysym in ("BackSpace", "Delete"):
            if self._raw:
                self._raw = self._raw[:-1]
                self._refresh_display()
            return "break"

        if keysym in ("Tab", "Return", "Escape"):
            return  # let these through normally

        # Ignore modifier-only keys
        if len(event.char) == 0:
            return "break"

        ch = event.char.upper()
        if re.match(r'[A-Z0-9]', ch) and len(self._raw) < self._MAX_RAW:
            self._raw += ch
            self._refresh_display()

        return "break"

    # ── Paste handler ─────────────────────────────────────────────────────
    def _on_paste(self, event=None):
        try:
            pasted = self.clipboard_get()
        except Exception:
            return "break"

        self._on_focus_in()
        # Strip everything except alphanumeric, then take only the payload
        # Accept both "AS-XXXX-XXXX-XXXX-XXXX" and raw "XXXXXXXXXXXXXXXX"
        clean = re.sub(r'[^A-Za-z0-9]', '', pasted).upper()
        # If it starts with "AS" strip that prefix
        if clean.startswith("AS"):
            clean = clean[2:]
        # Take max 16 chars
        self._raw = clean[:self._MAX_RAW]
        self._refresh_display()
        return "break"

    # ── Refresh the entry widget from self._raw ───────────────────────────
    def _refresh_display(self):
        display = self._format_display(self._raw)
        self._key_var.set(display)
        # Place cursor at end of typed content
        self._entry.icursor("end")

    # ── React to text changes ─────────────────────────────────────────────
    def _on_text_changed(self):
        if self._placeholder_active:
            return

        raw = self._raw
        n   = len(raw)
        segs_done = n // 4
        last_partial = n % 4

        # Update segment progress dots
        for i, dot in enumerate(self._seg_dots):
            if i < segs_done:
                dot.config(text="●", fg=ROYAL)
            elif i == segs_done and last_partial > 0:
                dot.config(text="◐", fg=ROYAL_LITE)
            else:
                dot.config(text="○", fg=MATTE_LITE)

        # Char count
        self._count_lbl.config(
            text=f"{n} / 16 characters",
            fg=SUCCESS if n == 16 else MATTE_LITE)

        # Border color feedback
        if n == 0:
            self._entry_border.config(bg=GREY_LINE)
            self._entry.config(fg=MATTE_DARK)
        elif n == 16:
            self._entry_border.config(bg=SUCCESS)
            self._entry.config(fg=SUCCESS)
        else:
            self._entry_border.config(bg=ROYAL_LITE)
            self._entry.config(fg=MATTE_DARK)

        # Clear any old error
        self._error_lbl.config(text="")

    # ── Activate ──────────────────────────────────────────────────────────
    def _on_activate(self):
        # Check lockout first (fix #8)
        locked, secs = activation_is_locked()
        if locked:
            mins = secs // 60
            self._error_lbl.config(
                text=f"⚠  Too many failed attempts.  Try again in {mins} min.")
            self._entry_border.config(bg=DANGER)
            self._act_btn.config(state="disabled")
            return

        if self._placeholder_active or len(self._raw) < 16:
            self._error_lbl.config(
                text="⚠  Key is incomplete — all 16 characters are required.")
            self._entry_border.config(bg=DANGER)
            return

        full_key = "AS-" + "-".join(
            self._raw[i:i+4] for i in range(0, 16, 4)
        )

        if not is_valid_key_format(full_key):
            activation_record_failure()
            locked, secs = activation_is_locked()
            if locked:
                mins = secs // 60
                self._error_lbl.config(
                    text=f"⚠  Too many failed attempts.  Locked for {mins} min.")
            else:
                remaining = MAX_FAILED_ACTIVATIONS - int(_reg_get("FailCount", "0"))
                self._error_lbl.config(
                    text=f"⚠  Invalid key.  {remaining} attempt(s) remaining.")
            self._entry_border.config(bg=DANGER)
            return

        try:
            save_license(full_key)
            activation_record_success()
        except Exception as ex:
            self._error_lbl.config(text=f"⚠  Could not save license: {ex}")
            return

        self.result = full_key
        self._entry_border.config(bg=SUCCESS)
        self._act_btn.config(text="✔  Activated!", bg=SUCCESS, state="disabled")
        self.after(700, self.destroy)


# ─────────────────────────────────────────────────────────────────────────────
# UnauthorizedScreen  — non-closable, covers everything
# ─────────────────────────────────────────────────────────────────────────────
class UnauthorizedScreen(tk.Toplevel):
    def __init__(self, parent, reason: str = "device"):
        super().__init__(parent)
        self.title("AlphaSense — Access Denied")
        self.configure(bg=DANGER)
        self.resizable(False, False)
        self.grab_set()
        self.focus_force()
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.attributes("-topmost", True)

        w, h = 520, 380
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w)//2}+{(sh - h)//2}")

        tk.Frame(self, bg=DANGER, height=24).pack()
        tk.Label(self, text="🔒",
                 font=("Segoe UI", 48), bg=DANGER, fg=WHITE).pack()
        tk.Frame(self, bg=DANGER, height=10).pack()

        if reason == "device":
            title_text  = "Unauthorized Device"
            detail_text = ("The connected device does not match the\n"
                           "product key registered on this installation.\n\n"
                           "Please connect the correct AlphaSense device\n"
                           "or contact your supplier.")
        else:
            title_text  = "Unauthorized User"
            detail_text = ("A valid AlphaSense product key is required\n"
                           "to use this software.")

        tk.Label(self, text=title_text,
                 font=("Segoe UI", 20, "bold"),
                 bg=DANGER, fg=WHITE).pack()
        tk.Frame(self, bg=DANGER, height=10).pack()
        tk.Label(self, text=detail_text,
                 font=("Segoe UI", 11),
                 bg=DANGER, fg="#FFCCCC",
                 justify="center").pack(padx=40)
        tk.Frame(self, bg=DANGER, height=16).pack()
        tk.Frame(self, bg="#A0291E", height=1).pack(fill="x", padx=40)
        tk.Frame(self, bg=DANGER, height=14).pack()
        tk.Label(self,
                 text="This window cannot be closed.\n"
                      "Disconnect the device and restart the application.",
                 font=("Segoe UI", 9),
                 bg=DANGER, fg="#FFAAAA",
                 justify="center").pack()

        self._keep_on_top()

    def _keep_on_top(self):
        self.lift()
        self.attributes("-topmost", True)
        self.after(500, self._keep_on_top)


# ─────────────────────────────────────────────────────────────────────────────
# DeviceManager  — uses saved product key for auth
# ─────────────────────────────────────────────────────────────────────────────
class DeviceManager:
    def __init__(self, log_cb, status_cb, version_cb, auth_cb, product_key: str):
        self.log         = log_cb
        self.status      = status_cb
        self.version_cb  = version_cb
        self.auth_cb     = auth_cb
        self._product_key = product_key   # the saved/entered key

        self._usb_serial  = None
        self._usb_port    = None
        self._usb_lock    = threading.Lock()
        self._usb_active  = False
        self._uploading   = False

        self._ble_client    = None
        self._ble_connected = False
        self._ble_lock      = threading.Lock()

        self._auth_nonce    = None
        self._auth_event    = threading.Event()
        self._auth_response = None

        self._running = False
        self._loop    = None

    # ── Per-connection auth context (fix #6 — eliminates USB/BLE race) ───
    def _new_auth_ctx(self):
        """Return a fresh {nonce, event, response} dict for one auth attempt."""
        return {"nonce": None, "event": threading.Event(), "response": None}

    def get_usb_port(self):
        with self._usb_lock:
            return self._usb_port

    def is_usb_active(self):
        with self._usb_lock:
            return self._usb_active

    def send_usb(self, text: str):
        with self._usb_lock:
            s = self._usb_serial
        if s and s.is_open:
            try:
                s.write((text + "\n").encode("utf-8", errors="replace"))
                return True
            except Exception as e:
                self.log(f"USB send error: {e}", "err")
        return False

    def start(self):
        self._running = True
        threading.Thread(target=self._usb_loop,  daemon=True).start()
        threading.Thread(target=self._ble_thread, daemon=True).start()

    def stop(self):
        self._running = False
        self._disconnect_usb()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def release_port_for_upload(self):
        with self._usb_lock:
            self._uploading = True
            try:
                if self._usb_serial and self._usb_serial.is_open:
                    self._usb_serial.close()
            except Exception:
                pass

    def reclaim_port_after_upload(self):
        with self._usb_lock:
            self._uploading = False

    # ── Auth helpers ──────────────────────────────────────────────────────
    def _do_auth(self, send_fn) -> bool:
        """USB auth — uses its own isolated context (fix #6)."""
        ctx = self._new_auth_ctx()
        nonce = secrets.token_hex(16)
        ctx["nonce"] = nonce
        # Store so _receive_auth_response can post to the right context
        self._usb_auth_ctx = ctx
        send_fn(f"AUTH_CHALLENGE:{nonce}")
        got = ctx["event"].wait(timeout=AUTH_TIMEOUT)
        self._usb_auth_ctx = None
        if not got or ctx["response"] is None:
            return False
        return verify_device_response(nonce, ctx["response"], self._product_key)

    def _receive_auth_response(self, response: str):
        """Route incoming AUTH_RESPONSE to the correct waiting context."""
        ctx = getattr(self, "_usb_auth_ctx", None)
        if ctx is not None:
            ctx["response"] = response
            ctx["event"].set()

    # USB ─────────────────────────────────────────────────────────────────
    def _usb_loop(self):
        while self._running:
            if self._uploading:
                time.sleep(USB_POLL_SEC)
                continue
            port = self._find_usb()
            with self._usb_lock:
                is_open = self._usb_serial is not None and self._usb_serial.is_open
            if port and not is_open:
                self._force_ble_off()
                self._connect_usb(port)
            elif not port and is_open:
                self.log("USB cable disconnected.", "dim")
                self.status("disconnected", None)
                self._disconnect_usb()
            elif is_open:
                try:
                    with self._usb_lock:
                        self._usb_serial.in_waiting
                except Exception:
                    self.log("USB connection lost.", "dim")
                    self.status("disconnected", None)
                    self._disconnect_usb()
            time.sleep(USB_POLL_SEC)

    def _find_usb(self):
        ports = serial.tools.list_ports.comports()
        for p in ports:
            haystack = " ".join(filter(None, [
                p.description, p.manufacturer, p.hwid
            ])).lower()
            if any(hint.lower() in haystack for hint in ESP32_USB_HINTS):
                return p.device
        for p in ports:
            desc = (p.description or "").lower()
            hwid = (p.hwid or "").lower()
            if "bluetooth" in desc or "bthenum" in hwid:
                continue
            if p.device == "COM1":
                continue
            if "usb" in hwid or "usb" in desc:
                return p.device
        return None

    def _connect_usb(self, port):
        try:
            s = serial.Serial(port, USB_BAUD_RATE, timeout=1)
            with self._usb_lock:
                self._usb_serial = s
                self._usb_port   = port
                self._usb_active = True
            self.log(f"USB connected on {port} — authenticating…", "dim")
            threading.Thread(target=self._usb_read, daemon=True).start()
            time.sleep(1.2)   # wait for device to boot/settle
            passed = self._do_auth(self.send_usb)
            if passed:
                self.log(f"Device verified  ·  {port}", "usb")
                self.status("connected", None)
                self.auth_cb(True)
            else:
                self.log("Auth FAILED — device key does not match.", "err")
                self.status("disconnected", None)
                self.auth_cb(False)
                self._disconnect_usb()
        except Exception as e:
            self.log(f"USB open error: {e}", "err")

    def _usb_read(self):
        while self._running:
            if self._uploading:
                time.sleep(0.5)
                continue
            with self._usb_lock:
                s = self._usb_serial
            if s is None or not s.is_open:
                break
            try:
                line = s.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("AUTH_RESPONSE:"):
                    self._receive_auth_response(line.split(":", 1)[1].strip())
                elif line.startswith("VERSION:"):
                    self.version_cb(line.split(":", 1)[1].strip())
                else:
                    self.log(line, "usb")
            except Exception:
                if not self._uploading:
                    self.log("USB connection lost.", "dim")
                    self.status("disconnected", None)
                    self._disconnect_usb()
                break

    def _disconnect_usb(self):
        with self._usb_lock:
            try:
                if self._usb_serial:
                    self._usb_serial.close()
            except Exception:
                pass
            self._usb_serial = None
            self._usb_port   = None
            self._usb_active = False

    # BLE ─────────────────────────────────────────────────────────────────
    def _ble_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ble_loop())

    async def _ble_loop(self):
        while self._running:
            if self._usb_active:
                await asyncio.sleep(BLE_POLL_SEC)
                continue
            with self._ble_lock:
                already = self._ble_connected
            if not already:
                device = await self._scan_ble()
                if device:
                    if not self._usb_active:
                        await self._connect_ble(device)
                else:
                    with self._ble_lock:
                        if self._ble_connected:
                            self._ble_connected = False
                            self.log("BLE device not found.", "dim")
                            self.status(None, "disconnected")
            await asyncio.sleep(BLE_POLL_SEC)

    async def _scan_ble(self):
        try:
            self.log("Scanning for BLE devices…", "dim")
            devices = await BleakScanner.discover(timeout=5)
            for d in devices:
                if d.name and d.name.startswith("AlphaSense"):
                    return d
        except Exception as e:
            self.log(f"BLE scan error: {e}", "err")
        return None

    async def _connect_ble(self, device):
        client = BleakClient(device.address,
                             disconnected_callback=self._on_ble_disconnect)
        try:
            await client.connect()
            if not client.is_connected:
                return
            with self._ble_lock:
                self._ble_client    = client
                self._ble_connected = True
            self.log(f"BLE connected  ·  {device.address} — authenticating…", "dim")
            await client.start_notify(NUS_TX_UUID, self._on_ble_data)

            # BLE auth — isolated context, no race with USB (fix #6)
            ble_ctx = self._new_auth_ctx()
            nonce = secrets.token_hex(16)
            ble_ctx["nonce"] = nonce
            self._ble_auth_ctx = ble_ctx
            await client.write_gatt_char(
                NUS_RX_UUID,
                f"AUTH_CHALLENGE:{nonce}\n".encode("utf-8"),
                response=False)
            for _ in range(AUTH_TIMEOUT * 10):
                if ble_ctx["event"].is_set():
                    break
                await asyncio.sleep(0.1)
            self._ble_auth_ctx = None

            passed = (ble_ctx["response"] is not None and
                      verify_device_response(nonce, ble_ctx["response"],
                                             self._product_key))
            if not passed:
                self.log("BLE auth FAILED — device key does not match.", "err")
                self.auth_cb(False)
                try:
                    await client.stop_notify(NUS_TX_UUID)
                    await client.disconnect()
                except Exception:
                    pass
                self._mark_ble_disconnected()
                return

            self.log(f"BLE device verified  ·  {device.address}", "bt")
            self.status(None, "connected")
            self.auth_cb(True)
            try:
                await client.write_gatt_char(
                    NUS_RX_UUID, "VERSION?\n".encode("utf-8"), response=False)
            except Exception:
                pass
            while self._running and client.is_connected and not self._usb_active:
                await asyncio.sleep(1)
            if self._usb_active and client.is_connected:
                self.log("USB connected — BLE paused.", "dim")
                self.status(None, "disconnected")
                try:
                    await client.stop_notify(NUS_TX_UUID)
                    await client.disconnect()
                except Exception:
                    pass
                self._mark_ble_disconnected()
                return
            try:
                await client.stop_notify(NUS_TX_UUID)
                await client.disconnect()
            except Exception:
                pass
        except Exception as e:
            self.log(f"BLE connect error: {e}", "err")
            self._mark_ble_disconnected()

    def _on_ble_data(self, sender, data):
        msg = data.decode("utf-8", errors="replace").strip()
        if not msg:
            return
        if msg.startswith("AUTH_RESPONSE:"):
            response = msg.split(":", 1)[1].strip()
            # Route to BLE auth context (fix #6)
            ble_ctx = getattr(self, "_ble_auth_ctx", None)
            if ble_ctx is not None:
                ble_ctx["response"] = response
                ble_ctx["event"].set()
        elif msg.startswith("VERSION:"):
            self.version_cb(msg.split(":", 1)[1].strip())
        else:
            self.log(msg, "bt")

    def _on_ble_disconnect(self, client):
        self.log("BLE disconnected.", "dim")
        self.status(None, "disconnected")
        self._mark_ble_disconnected()

    def _mark_ble_disconnected(self):
        with self._ble_lock:
            self._ble_connected = False
            self._ble_client    = None

    def _force_ble_off(self):
        with self._ble_lock:
            client = self._ble_client
        if client and client.is_connected:
            asyncio.run_coroutine_threadsafe(client.disconnect(), self._loop)
        self._mark_ble_disconnected()
        self.status(None, "disconnected")


# ─────────────────────────────────────────────────────────────────────────────
# UpdateDialog
# ─────────────────────────────────────────────────────────────────────────────
class UpdateDialog(tk.Toplevel):
    def __init__(self, parent, port, on_update, on_cancel):
        super().__init__(parent)
        self.title("Firmware Update")
        self.configure(bg=WHITE)
        self.resizable(False, False)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", on_cancel)
        self.geometry("520x400")
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - 520) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 400) // 2
        self.geometry(f"+{px}+{py}")
        tk.Frame(self, bg=ROYAL, height=6).pack(fill="x")
        tk.Frame(self, bg=WHITE, height=12).pack()
        tk.Label(self, text="Firmware Update Available",
                 font=("Segoe UI", 14, "bold"), bg=WHITE, fg=MATTE_DARK).pack()
        tk.Label(self, text="Your device is running  v1.0  →  v2.0 is available",
                 font=F_BODY_N, bg=WHITE, fg=MATTE_MID).pack(pady=(4, 12))
        warn_outer = tk.Frame(self, bg=WARN_BG, bd=0,
                              highlightbackground=WARN, highlightthickness=1)
        warn_outer.pack(fill="x", padx=28, pady=4)
        tk.Label(warn_outer, text="⚠   Before You Proceed",
                 font=F_H2, bg=WARN_BG, fg=WARN).pack(anchor="w", padx=14, pady=(10,4))
        for txt in ["1.  Ensure the USB cable is firmly connected.",
                    "2.  Hold the  BOOT  button on your AlphaSense.",
                    "3.  Release BOOT once the upload begins."]:
            tk.Label(warn_outer, text=txt, font=F_BODY_N, bg=WARN_BG,
                     fg=MATTE_DARK, justify="left").pack(anchor="w", padx=24, pady=2)
        tk.Frame(warn_outer, height=10, bg=WARN_BG).pack()
        tk.Label(self, text=f"Target port:  {port}",
                 font=F_MONO_B, bg=WHITE, fg=MATTE_MID).pack(pady=(8, 4))
        btn_row = tk.Frame(self, bg=WHITE)
        btn_row.pack(pady=14)
        tk.Button(btn_row, text="Cancel", font=F_BODY,
                  bg=GREY_BG2, fg=MATTE_MID, activebackground=GREY_LINE,
                  relief="flat", bd=0, padx=22, pady=9, cursor="hand2",
                  command=on_cancel).pack(side="left", padx=10)
        tk.Button(btn_row, text="  ⬆   Update to v2.0  ",
                  font=("Segoe UI", 10, "bold"),
                  bg=ROYAL, fg=WHITE, activebackground=ROYAL_DARK,
                  relief="flat", bd=0, padx=22, pady=9, cursor="hand2",
                  command=on_update).pack(side="left", padx=10)


# ─────────────────────────────────────────────────────────────────────────────
# UploadDialog  — 4-phase progress: Check → Download → Compile → Upload
# ─────────────────────────────────────────────────────────────────────────────
class UploadDialog(tk.Toplevel):
    _PHASES = ["Check", "Download", "Compile", "Upload"]

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Firmware Update")
        self.configure(bg=WHITE)
        self.resizable(False, False)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.geometry("520x340")
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - 520) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 340) // 2
        self.geometry(f"+{px}+{py}")

        # Header
        tk.Frame(self, bg=ROYAL, height=6).pack(fill="x")
        tk.Frame(self, bg=WHITE, height=14).pack()
        tk.Label(self, text="Firmware Update",
                 font=("Segoe UI", 13, "bold"), bg=WHITE, fg=MATTE_DARK).pack()
        tk.Label(self, text="Do not disconnect the device during the update.",
                 font=F_BODY_N, bg=WHITE, fg=MATTE_MID).pack(pady=(3, 16))

        # ── Phase stepper ──────────────────────────────────────────────────
        stepper_frame = tk.Frame(self, bg=WHITE)
        stepper_frame.pack(fill="x", padx=40, pady=(0, 14))

        self._phase_lbls = []
        self._phase_dots = []
        for i, name in enumerate(self._PHASES):
            col = tk.Frame(stepper_frame, bg=WHITE)
            col.pack(side="left", expand=True)

            dot = tk.Label(col, text="○",
                           font=("Segoe UI", 14, "bold"),
                           bg=WHITE, fg=MATTE_LITE)
            dot.pack()
            lbl = tk.Label(col, text=name,
                           font=("Segoe UI", 8, "bold"),
                           bg=WHITE, fg=MATTE_LITE)
            lbl.pack()

            self._phase_dots.append(dot)
            self._phase_lbls.append(lbl)

            # Connector line between phases
            if i < len(self._PHASES) - 1:
                tk.Frame(stepper_frame, bg=GREY_LINE,
                         height=2, width=20).pack(side="left", expand=True,
                                                   fill="x", pady=10)

        # ── Progress bar ──────────────────────────────────────────────────
        bar_frame = tk.Frame(self, bg=WHITE)
        bar_frame.pack(fill="x", padx=40, pady=(0, 6))

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Upload.Horizontal.TProgressbar",
                        troughcolor=GREY_BG2, background=ROYAL,
                        bordercolor=GREY_LINE, lightcolor=ROYAL_LITE,
                        darkcolor=ROYAL_DARK, thickness=16)
        self._pbar = ttk.Progressbar(bar_frame,
                                     style="Upload.Horizontal.TProgressbar",
                                     orient="horizontal", length=440,
                                     mode="indeterminate")
        self._pbar.pack(fill="x")
        self._pbar.start(12)

        # ── Status labels ─────────────────────────────────────────────────
        self._phase_lbl = tk.Label(self, text="",
                                   font=("Segoe UI", 10, "bold"),
                                   bg=WHITE, fg=ROYAL)
        self._phase_lbl.pack()

        self._status_lbl = tk.Label(self, text="Please wait…",
                                    font=F_SMALL_N, bg=WHITE, fg=MATTE_MID)
        self._status_lbl.pack(pady=(3, 0))

        tk.Frame(self, bg=WHITE, height=10).pack()

        self._current_phase_idx = -1

    # ── Public API ────────────────────────────────────────────────────────
    def advance_phase(self, phase_name: str):
        """Mark a phase as active; previous phases shown as complete."""
        idx = next((i for i, n in enumerate(self._PHASES)
                    if n.lower() == phase_name.lower()), None)
        if idx is None:
            return
        self._current_phase_idx = idx
        for i, (dot, lbl) in enumerate(zip(self._phase_dots, self._phase_lbls)):
            if i < idx:
                dot.config(text="●", fg=SUCCESS)
                lbl.config(fg=SUCCESS)
            elif i == idx:
                dot.config(text="◉", fg=ROYAL)
                lbl.config(fg=ROYAL)
            else:
                dot.config(text="○", fg=MATTE_LITE)
                lbl.config(fg=MATTE_LITE)
        self._phase_lbl.config(text=f"{phase_name}…", fg=ROYAL)

    def set_phase(self, text, color=ROYAL):
        self._phase_lbl.config(text=text, fg=color)

    def set_status(self, msg, color=MATTE_MID):
        self._status_lbl.config(text=msg, fg=color)

    def set_determinate(self, value: int):
        self._pbar.stop()
        self._pbar.config(mode="determinate", value=value)

    def set_indeterminate(self):
        self._pbar.config(mode="indeterminate")
        self._pbar.start(12)

    def finish(self, success: bool):
        self._pbar.stop()
        c = SUCCESS if success else DANGER
        ttk.Style(self).configure("Upload.Horizontal.TProgressbar", background=c)
        self._pbar.config(mode="determinate", value=100 if success else 0)
        if success:
            for dot, lbl in zip(self._phase_dots, self._phase_lbls):
                dot.config(text="●", fg=SUCCESS)
                lbl.config(fg=SUCCESS)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────
class Dashboard(tk.Tk):
    def __init__(self, product_key: str):
        super().__init__()
        self._product_key      = product_key
        self.title("AlphaSense")
        self.geometry("820x560")
        self.minsize(700, 460)
        self.configure(bg=OFF_WHITE)

        self._detected_version = None
        self._update_notified  = False
        self._usb_connected    = False
        self._msg_counts       = {"usb": 0, "bt": 0, "err": 0, "dim": 0, "sys": 0}
        self._start_time       = time.time()
        self._update_dlg_open  = False
        self._last_msg         = {"usb": "–", "bt": "–"}
        self._auth_screen      = None

        # Tray state
        self._tray_icon        = None
        self._tray_status      = "idle"
        self._minimize_notified = False   # show balloon only once

        self._apply_ttk_style()
        self._build_ui()

        self._manager = DeviceManager(
            log_cb      = self._log,
            status_cb   = self._update_status,
            version_cb  = self._on_version_received,
            auth_cb     = self._on_auth_result,
            product_key = product_key,
        )
        self._manager.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick_uptime()
        # Silently fetch manifest to get real latest version from server
        self._check_manifest_in_background()
        # Start system tray icon
        self._start_tray()

    # ── TTK style ─────────────────────────────────────────────────────────
    def _apply_ttk_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Vertical.TScrollbar",
                    background=GREY_BG2, troughcolor=OFF_WHITE,
                    arrowcolor=MATTE_LITE, bordercolor=OFF_WHITE, relief="flat")

    # ── Auth result ───────────────────────────────────────────────────────
    def _on_auth_result(self, passed: bool):
        def _handle():
            if passed:
                if self._auth_screen and self._auth_screen.winfo_exists():
                    self._auth_screen.destroy()
                    self._auth_screen = None
                self._tray_set_status("connected")
                _play_sound("connect")
            else:
                if self._auth_screen and self._auth_screen.winfo_exists():
                    return
                self._auth_screen = UnauthorizedScreen(self, "device")
                self._tray_set_status("unauthorized")
                _play_sound("auth_fail")
        self.after(0, _handle)

    # ── Build UI ──────────────────────────────────────────────────────────
    def _build_ui(self):
        # HEADER
        hdr = tk.Frame(self, bg=ROYAL)
        hdr.pack(fill="x")
        hdr_inner = tk.Frame(hdr, bg=ROYAL, padx=24, pady=16)
        hdr_inner.pack(fill="x")
        left = tk.Frame(hdr_inner, bg=ROYAL)
        left.pack(side="left")
        tk.Label(left, text="AlphaSense",
                 font=("Segoe UI", 16, "bold"), bg=ROYAL, fg=WHITE).pack(anchor="w")
        tk.Label(left, text="USB Priority  ·  BLE Fallback  ·  OTA Update",
                 font=("Segoe UI", 9), bg=ROYAL, fg="#A8C0FF").pack(anchor="w", pady=(2, 0))
        right = tk.Frame(hdr_inner, bg=ROYAL)
        right.pack(side="right", anchor="center")
        # Show masked product key in header badge
        masked = self._product_key[:6] + "-****-****"
        self._fw_badge = tk.Label(right,
                                  text=f"Firmware  –   ·   {masked}",
                                  font=("Consolas", 9, "bold"),
                                  bg="#1A3AB8", fg="#A8C0FF",
                                  padx=14, pady=6, relief="flat")
        self._fw_badge.pack()
        tk.Frame(self, bg=ROYAL_DARK, height=3).pack(fill="x")

        # FIRMWARE VERSION BAR
        self._ver_bar = tk.Frame(self, bg=ROYAL_XPALE, pady=8)
        self._ver_bar.pack(fill="x")
        tk.Frame(self, bg=GREY_LINE, height=1).pack(fill="x")
        ver_inner = tk.Frame(self._ver_bar, bg=ROYAL_XPALE)
        ver_inner.pack(fill="x", padx=24)
        self._ver_icon = tk.Label(ver_inner, text="◉",
                                  font=("Segoe UI", 10, "bold"),
                                  bg=ROYAL_XPALE, fg=MATTE_LITE)
        self._ver_icon.pack(side="left", padx=(0, 8))
        self._ver_lbl = tk.Label(ver_inner, text="Firmware version:  not detected",
                                 font=F_BODY, bg=ROYAL_XPALE, fg=MATTE_MID)
        self._ver_lbl.pack(side="left")
        self._upd_btn = tk.Button(ver_inner, text="  ⬆   Update to v2.0  ",
                                  font=("Segoe UI", 9, "bold"),
                                  bg=WARN, fg=WHITE, activebackground="#A06000",
                                  relief="flat", bd=0, padx=12, pady=5, cursor="hand2",
                                  command=self._show_update_dialog)

        # MAIN CONTENT
        content = tk.Frame(self, bg=OFF_WHITE, padx=24, pady=18)
        content.pack(fill="both", expand=True)

        tk.Label(content, text="CONNECTION STATUS",
                 font=("Segoe UI", 8, "bold"),
                 bg=OFF_WHITE, fg=MATTE_LITE).pack(anchor="w", pady=(0, 8))
        cards_row = tk.Frame(content, bg=OFF_WHITE)
        cards_row.pack(fill="x", pady=(0, 16))
        self._usb_card = self._build_card(cards_row, "USB", "🔌", "Cable Connection", ROYAL)
        self._bt_card  = self._build_card(cards_row, "BLUETOOTH", "📶", "Wireless BLE", ROYAL)
        self._usb_card.pack(side="left", padx=(0, 14), fill="x", expand=True)
        self._bt_card.pack(side="left", fill="x", expand=True)

        stats_row = tk.Frame(content, bg=OFF_WHITE)
        stats_row.pack(fill="x", pady=(0, 16))
        self._stat_labels = {}
        for tag, label, fg_c, bg_c in [
            ("usb", "USB Messages",  ROYAL,     ROYAL_PALE),
            ("bt",  "BLE Messages",  "#1A7FC1", "#E6F3FB"),
            ("err", "Errors",         DANGER,   DANGER_BG),
        ]:
            chip = tk.Frame(stats_row, bg=bg_c,
                            highlightbackground=GREY_LINE, highlightthickness=1)
            chip.pack(side="left", padx=(0, 10), fill="y")
            inner = tk.Frame(chip, bg=bg_c, padx=14, pady=10)
            inner.pack()
            val_lbl = tk.Label(inner, text="0", font=F_CARD_VAL, bg=bg_c, fg=fg_c)
            val_lbl.pack()
            tk.Label(inner, text=label, font=F_CARD_LBL, bg=bg_c, fg=MATTE_MID).pack()
            self._stat_labels[tag] = val_lbl

        tk.Frame(content, bg=GREY_LINE, height=1).pack(fill="x", pady=(0, 14))

        # DEVICE STATUS PANEL
        tk.Label(content, text="DEVICE STATUS",
                 font=("Segoe UI", 8, "bold"),
                 bg=OFF_WHITE, fg=MATTE_LITE).pack(anchor="w", pady=(0, 8))
        ds_outer = tk.Frame(content, bg=CARD_BG,
                            highlightbackground=GREY_LINE, highlightthickness=1)
        ds_outer.pack(fill="both", expand=True)
        ds_inner = tk.Frame(ds_outer, bg=CARD_BG, padx=20, pady=16)
        ds_inner.pack(fill="both", expand=True)
        row1 = tk.Frame(ds_inner, bg=CARD_BG)
        row1.pack(fill="x", pady=(0, 10))
        self._ds_dot = tk.Label(row1, text="●",
                                font=("Segoe UI", 20, "bold"),
                                bg=CARD_BG, fg=MATTE_LITE)
        self._ds_dot.pack(side="left", padx=(0, 14))
        ds_text = tk.Frame(row1, bg=CARD_BG)
        ds_text.pack(side="left", anchor="center")
        self._ds_state = tk.Label(ds_text, text="No device connected",
                                  font=("Segoe UI", 13, "bold"),
                                  bg=CARD_BG, fg=MATTE_LITE)
        self._ds_state.pack(anchor="w")
        self._ds_detail = tk.Label(ds_text,
                                   text="Waiting for USB or Bluetooth connection…",
                                   font=F_BODY_N, bg=CARD_BG, fg=MATTE_LITE)
        self._ds_detail.pack(anchor="w")
        tk.Frame(ds_inner, bg=GREY_LINE, height=1).pack(fill="x", pady=(4, 10))
        grid = tk.Frame(ds_inner, bg=CARD_BG)
        grid.pack(fill="x")

        def _info_cell(parent, col, lbl_text, attr):
            cell = tk.Frame(parent, bg=CARD_BG)
            cell.grid(row=0, column=col, sticky="w", padx=(0, 40))
            tk.Label(cell, text=lbl_text, font=F_SMALL, bg=CARD_BG,
                     fg=MATTE_LITE).pack(anchor="w")
            lbl = tk.Label(cell, text="–", font=F_BODY, bg=CARD_BG, fg=MATTE_DARK)
            lbl.pack(anchor="w")
            setattr(self, attr, lbl)

        _info_cell(grid, 0, "CONNECTION TYPE", "_ds_type")
        _info_cell(grid, 1, "PORT / ADDRESS",  "_ds_port")
        _info_cell(grid, 2, "FIRMWARE",         "_ds_fw")
        _info_cell(grid, 3, "AUTH STATUS",      "_ds_auth")

        act_row = tk.Frame(ds_inner, bg=CARD_BG)
        act_row.pack(fill="x", pady=(14, 0))
        self._ds_activity = tk.Label(act_row, text="○  No activity",
                                     font=F_SMALL_N, bg=CARD_BG, fg=MATTE_LITE)
        self._ds_activity.pack(side="left")
        self._ds_msgcount = tk.Label(act_row, text="",
                                     font=F_SMALL_N, bg=CARD_BG, fg=MATTE_LITE)
        self._ds_msgcount.pack(side="right")

        # STATUS BAR
        tk.Frame(self, bg=GREY_LINE, height=1).pack(fill="x")
        sbar = tk.Frame(self, bg=CARD_SH, pady=6)
        sbar.pack(fill="x")
        sbar_inner = tk.Frame(sbar, bg=CARD_SH)
        sbar_inner.pack(fill="x", padx=24)
        self._port_lbl = tk.Label(sbar_inner, text="Port:  –",
                                  font=F_STATUS, bg=CARD_SH, fg=MATTE_MID)
        self._port_lbl.pack(side="left")
        tk.Label(sbar_inner, text="  |  ", font=F_STATUS,
                 bg=CARD_SH, fg=GREY_LINE).pack(side="left")
        self._total_lbl = tk.Label(sbar_inner, text="Messages:  0",
                                   font=F_STATUS, bg=CARD_SH, fg=MATTE_MID)
        self._total_lbl.pack(side="left")
        self._uptime_lbl = tk.Label(sbar_inner, text="Uptime:  00:00:00",
                                    font=F_STATUS, bg=CARD_SH, fg=MATTE_MID)
        self._uptime_lbl.pack(side="right")

    # ── Status card ───────────────────────────────────────────────────────
    def _build_card(self, parent, label, icon, subtitle, color):
        frame = tk.Frame(parent, bg=CARD_BG,
                         highlightbackground=GREY_LINE, highlightthickness=1)
        frame._color  = color
        frame._active = False
        inner = tk.Frame(frame, bg=CARD_BG, padx=20, pady=16)
        inner.pack(fill="both", expand=True)
        top = tk.Frame(inner, bg=CARD_BG)
        top.pack(anchor="w", fill="x")
        ico_lbl = tk.Label(top, text=icon, font=("Segoe UI", 14),
                           bg=CARD_BG, fg=MATTE_LITE)
        ico_lbl.pack(side="left", padx=(0, 10))
        right_top = tk.Frame(top, bg=CARD_BG)
        right_top.pack(side="left", anchor="center")
        tk.Label(right_top, text=label,
                 font=("Segoe UI", 10, "bold"), bg=CARD_BG, fg=MATTE_DARK).pack(anchor="w")
        tk.Label(right_top, text=subtitle,
                 font=F_SMALL_N, bg=CARD_BG, fg=MATTE_LITE).pack(anchor="w")
        tk.Frame(inner, bg=GREY_LINE, height=1).pack(fill="x", pady=(12, 10))
        status_row = tk.Frame(inner, bg=CARD_BG)
        status_row.pack(anchor="w", fill="x")
        dot = tk.Label(status_row, text="●",
                       font=("Segoe UI", 16, "bold"), bg=CARD_BG, fg=MATTE_LITE)
        dot.pack(side="left", padx=(0, 8))
        info = tk.Frame(status_row, bg=CARD_BG)
        info.pack(side="left", anchor="center")
        state_lbl = tk.Label(info, text="Disconnected",
                             font=("Segoe UI", 12, "bold"), bg=CARD_BG, fg=MATTE_LITE)
        state_lbl.pack(anchor="w")
        detail_lbl = tk.Label(info, text="–",
                              font=F_SMALL_N, bg=CARD_BG, fg=MATTE_LITE)
        detail_lbl.pack(anchor="w")
        frame._dot    = dot
        frame._state  = state_lbl
        frame._detail = detail_lbl
        frame._ico    = ico_lbl
        return frame

    def _set_card(self, card, connected: bool, detail: str = "–"):
        if connected:
            card.config(highlightbackground=card._color)
            card._dot.config(fg=card._color)
            card._state.config(text="Connected", fg=card._color)
            card._ico.config(fg=card._color)
        else:
            card.config(highlightbackground=GREY_LINE)
            card._dot.config(fg=MATTE_LITE)
            card._state.config(text="Disconnected", fg=MATTE_LITE)
            card._ico.config(fg=MATTE_LITE)
        card._detail.config(text=detail, fg=MATTE_MID if connected else MATTE_LITE)

    # ── Device Status helpers ─────────────────────────────────────────────
    def _ds_set_connected(self, conn_type, port_addr, fw):
        color = ROYAL if conn_type == "USB" else "#1A7FC1"
        self._ds_dot.config(fg=color)
        self._ds_state.config(text=f"Connected via {conn_type}", fg=color)
        self._ds_detail.config(text="Device authenticated and active.", fg=MATTE_MID)
        self._ds_type.config(text=conn_type, fg=color)
        self._ds_port.config(text=port_addr or "–", fg=MATTE_DARK)
        self._ds_fw.config(text=fw or "–", fg=MATTE_DARK)
        self._ds_auth.config(text="✔  Verified", fg=SUCCESS)
        self._ds_activity.config(text="● Active", fg=color)

    def _ds_set_disconnected(self):
        self._ds_dot.config(fg=MATTE_LITE)
        self._ds_state.config(text="No device connected", fg=MATTE_LITE)
        self._ds_detail.config(
            text="Waiting for USB or Bluetooth connection…", fg=MATTE_LITE)
        for attr in ("_ds_type", "_ds_port", "_ds_fw", "_ds_auth"):
            getattr(self, attr).config(text="–", fg=MATTE_DARK)
        self._ds_activity.config(text="○  No activity", fg=MATTE_LITE)
        self._ds_msgcount.config(text="")

    def _ds_set_uploading(self, phase):
        self._ds_dot.config(fg=WARN)
        self._ds_state.config(text=f"⟳  {phase}", fg=WARN)
        self._ds_detail.config(text="Do not disconnect the USB cable.", fg=WARN)
        self._ds_activity.config(text="● Upload in progress…", fg=WARN)
        self._tray_set_status("uploading")

    # ── Logging ───────────────────────────────────────────────────────────
    def _log(self, msg: str, tag: str = ""):
        def _w():
            t = tag if tag in self._msg_counts else "sys"
            self._msg_counts[t] = self._msg_counts.get(t, 0) + 1
            total = sum(self._msg_counts.values())
            self._total_lbl.config(text=f"Messages:  {total}")
            for k, lbl in self._stat_labels.items():
                lbl.config(text=str(self._msg_counts.get(k, 0)))
            if tag in ("usb", "bt"):
                self._ds_msgcount.config(
                    text=f"Total messages: {total}", fg=MATTE_LITE)
        self.after(0, _w)

    # ── Status callbacks ──────────────────────────────────────────────────
    def _update_status(self, usb_state, bt_state):
        def _a():
            port = self._manager.get_usb_port()
            if usb_state == "connected":
                self._set_card(self._usb_card, True, detail=port or "")
                self._usb_connected = True
                self._port_lbl.config(text=f"Port:  {port or '–'}", fg=ROYAL)
                fw = f"v{self._detected_version}" if self._detected_version else "detecting…"
                self._ds_set_connected("USB", port or "–", fw)
                self._tray_set_status("connecting")   # connecting until auth passes
            elif usb_state == "disconnected":
                self._set_card(self._usb_card, False)
                self._usb_connected = False
                self._upd_btn.pack_forget()
                self._update_notified  = False
                self._detected_version = None
                self._ver_lbl.config(text="Firmware version:  not detected", fg=MATTE_MID)
                self._ver_icon.config(fg=MATTE_LITE)
                fw_text = f"Firmware  –   ·   {self._product_key[:6]}-****-****"
                self._fw_badge.config(text=fw_text, fg="#A8C0FF")
                self._port_lbl.config(text="Port:  –", fg=MATTE_MID)
                if not self._manager._ble_connected:
                    self._ds_set_disconnected()
                    self._tray_set_status("idle")
                    _play_sound("disconnect")
            if bt_state == "connected":
                self._set_card(self._bt_card, True)
                if not self._usb_connected:
                    fw = f"v{self._detected_version}" if self._detected_version else "detecting…"
                    self._ds_set_connected("Bluetooth", "BLE", fw)
                    self._tray_set_status("connecting")
            elif bt_state == "disconnected":
                self._set_card(self._bt_card, False)
                if not self._usb_connected:
                    self._ds_set_disconnected()
                    self._tray_set_status("idle")
                    _play_sound("disconnect")
        self.after(0, _a)

    # ── Version handling ──────────────────────────────────────────────────
    def _on_version_received(self, version: str):
        def _handle():
            self._detected_version = version
            # Compare against server manifest version (thread-safe, fix #14)
            latest = get_latest_version()
            is_latest = not ota_version_is_newer(latest, version)
            color = SUCCESS if is_latest else WARN
            self._ver_lbl.config(text=f"Firmware version:  v{version}", fg=color)
            self._ver_icon.config(fg=color)
            masked = self._product_key[:6] + "-****-****"
            self._fw_badge.config(
                text=f"Firmware  v{version}   ·   {masked}",
                fg=WHITE if is_latest else "#FFE58A")
            self._log(f"Firmware version detected:  v{version}", "sys")
            self._ds_fw.config(text=f"v{version}",
                               fg=SUCCESS if is_latest else WARN)
            if not is_latest and not self._update_notified:
                self._update_notified = True
                self._upd_btn.config(
                    text=f"  ⬆   Update to v{latest}  ")
                self._upd_btn.pack(side="right", padx=10)
                if self._usb_connected:
                    self.after(900, self._show_update_dialog)
        self.after(0, _handle)

    def _check_manifest_in_background(self):
        """Fetch manifest silently on startup to get the real latest version."""
        def _fetch():
            manifest = ota_fetch_manifest()
            if manifest:
                remote_ver = manifest.get("latest_version", "")
                if remote_ver:
                    set_latest_version(remote_ver)   # thread-safe, fix #14
                    if self._detected_version:
                        self._on_version_received(self._detected_version)
        threading.Thread(target=_fetch, daemon=True).start()

    # ── Update flow ───────────────────────────────────────────────────────
    def _show_update_dialog(self):
        if self._update_dlg_open:
            return
        if not self._usb_connected:
            messagebox.showwarning("USB Required",
                                   "Firmware updates require a USB connection.\n"
                                   "Connect the AlphaSense via USB and try again.",
                                   parent=self)
            return
        port = self._manager.get_usb_port()
        if not port:
            messagebox.showwarning("No Port",
                                   "Cannot determine USB port.\n"
                                   "Please reconnect the AlphaSense.",
                                   parent=self)
            return
        self._update_dlg_open = True
        UpdateDialog(self, port,
                     on_update=self._do_on_update,
                     on_cancel=self._do_on_cancel)

    def _do_on_update(self):
        for w in self.winfo_children():
            if isinstance(w, UpdateDialog):
                w.destroy()
        self._update_dlg_open = False
        port = self._manager.get_usb_port()
        if port:
            self._start_update(port)

    def _do_on_cancel(self):
        for w in self.winfo_children():
            if isinstance(w, UpdateDialog):
                w.destroy()
        self._update_dlg_open = False

    # ── Upload ────────────────────────────────────────────────────────────
    def _start_update(self, port):
        dlg = UploadDialog(self)
        self._ds_set_uploading("Checking for update…")

        def _do():
            cli = ARDUINO_CLI
            if not os.path.isfile(cli):
                dlg.after(0, lambda: self._upload_failed(
                    dlg, f"arduino-cli not found:\n{cli}"))
                return

            # ── Phase 1: CHECK ────────────────────────────────────────────
            dlg.after(0, lambda: dlg.advance_phase("Check"))
            dlg.after(0, lambda: dlg.set_status("Checking server for latest firmware…"))
            dlg.after(0, lambda: dlg.set_indeterminate())

            manifest = ota_fetch_manifest()
            sketch_dir = None

            if manifest:
                remote_ver = manifest.get("latest_version", "")
                changelog  = manifest.get("changelog", "")
                fw_url     = manifest.get("firmware_url", "")
                fw_sha256  = manifest.get("firmware_sha256", "")

                # Update LATEST_VERSION dynamically from server (thread-safe, fix #14)
                if remote_ver:
                    set_latest_version(remote_ver)

                dlg.after(0, lambda v=remote_ver:
                    dlg.set_status(f"Latest firmware on server:  v{v}"))

                # ── Phase 2: DOWNLOAD ─────────────────────────────────────
                dlg.after(0, lambda: dlg.advance_phase("Download"))
                self.after(0, lambda: self._ds_set_uploading("Downloading firmware…"))

                def _progress(pct, label):
                    if pct >= 0:
                        dlg.after(0, lambda p=pct: dlg.set_determinate(p))
                    dlg.after(0, lambda l=label: dlg.set_status(l))

                zip_path = ota_download_firmware(fw_url, _progress)

                if not zip_path:
                    dlg.after(0, lambda: dlg.set_status(
                        "Download failed — using local firmware package."))
                    time.sleep(1.2)
                else:
                    # SHA-256 check — warn if not provided (fix #5)
                    if not fw_sha256:
                        dlg.after(0, lambda: dlg.set_status(
                            "Warning: no SHA-256 in manifest — skipping integrity check."))
                        time.sleep(0.8)
                    elif not ota_verify_sha256(zip_path, fw_sha256):
                        dlg.after(0, lambda: self._upload_failed(
                            dlg, "Download integrity check failed (SHA-256 mismatch).\n"
                                 "File may be corrupted or tampered. Please try again."))
                        return

                    # Extract
                    dlg.after(0, lambda: dlg.set_status("Extracting firmware package…"))
                    dlg.after(0, lambda: dlg.set_indeterminate())
                    sketch_dir = ota_extract_firmware(zip_path)
                    if not sketch_dir:
                        dlg.after(0, lambda: dlg.set_status(
                            "Extraction failed — using local firmware package."))
                        time.sleep(1.0)
                        sketch_dir = None

            # Fall back to local sketch if no download or no manifest
            if sketch_dir is None:
                sketch_dir = SKETCH_V2DIR

            if not os.path.isdir(sketch_dir):
                dlg.after(0, lambda: self._upload_failed(
                    dlg, f"Firmware folder not found:\n{sketch_dir}"))
                return

            # ── Phase 3: COMPILE ──────────────────────────────────────────
            dlg.after(0, lambda: dlg.advance_phase("Compile"))
            dlg.after(0, lambda: dlg.set_status("Compiling — this may take a minute…"))
            dlg.after(0, lambda: dlg.set_indeterminate())
            self.after(0, lambda: self._ds_set_uploading("Compiling sketch…"))

            ok = self._run_cli_silent([cli, "compile", "--fqbn", FQBN, sketch_dir])
            if not ok:
                dlg.after(0, lambda: self._upload_failed(dlg, "Compilation failed."))
                self._manager.reclaim_port_after_upload()
                return

            # ── Phase 4: UPLOAD ───────────────────────────────────────────
            dlg.after(0, lambda: dlg.advance_phase("Upload"))
            dlg.after(0, lambda: dlg.set_status(
                "Hold the BOOT button on your AlphaSense NOW!"))
            dlg.after(0, lambda: dlg.set_determinate(0))
            self.after(0, lambda: self._ds_set_uploading("Uploading to device…"))

            self._manager.release_port_for_upload()
            time.sleep(0.6)

            ok = self._run_cli_progress(
                [cli, "upload", "--fqbn", FQBN, "--port", port, sketch_dir], dlg)
            self._manager.reclaim_port_after_upload()

            if ok:
                new_ver = get_latest_version()   # thread-safe, fix #14
                dlg.after(0, lambda: self._upload_success(dlg, new_ver))

        threading.Thread(target=_do, daemon=True).start()

    def _run_cli_silent(self, cmd) -> bool:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    def _run_cli_progress(self, cmd, dlg) -> bool:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            lines_seen = 0
            EXPECTED   = 30
            for line in proc.stdout:
                lines_seen += 1
                pct = min(int(lines_seen / EXPECTED * 100), 95)
                dlg.after(0, lambda v=pct: dlg.set_determinate(v))
            proc.wait()
            if proc.returncode != 0:
                dlg.after(0, lambda: self._upload_failed(
                    dlg, f"Upload failed (exit {proc.returncode})"))
                return False
            return True
        except FileNotFoundError:
            dlg.after(0, lambda: self._upload_failed(dlg, f"Cannot run: {cmd[0]}"))
            return False
        except Exception as e:
            dlg.after(0, lambda: self._upload_failed(dlg, str(e)))
            return False

    def _upload_success(self, dlg, new_version: str = ""):
        dlg.finish(success=True)
        dlg.set_phase("Update complete!", SUCCESS)
        dlg.set_status(f"AlphaSense is now running firmware v{new_version}.")
        self._upd_btn.pack_forget()
        self._ver_lbl.config(
            text=f"Firmware version:  v{new_version}  (up to date)", fg=SUCCESS)
        port = self._manager.get_usb_port()
        self.after(0, lambda: self._ds_set_connected(
            "USB", port or "–", f"v{new_version}"))
        self._tray_set_status("connected")
        _play_sound("upload_ok")
        # Tray balloon notification
        if _TRAY_AVAILABLE and self._tray_icon:
            try:
                self._tray_icon.notify(
                    "Firmware Updated",
                    f"AlphaSense is now running v{new_version}."
                )
            except Exception:
                pass
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        tk.Button(dlg, text="  Close  ", font=F_BODY,
                  bg=SUCCESS, fg=WHITE, activebackground="#157A4A",
                  relief="flat", bd=0, padx=16, pady=8,
                  command=dlg.destroy).pack(pady=(10, 14))

    def _upload_failed(self, dlg, reason):
        dlg.finish(success=False)
        dlg.set_phase("Upload failed", DANGER)
        dlg.set_status(reason)
        _play_sound("upload_fail")
        port = self._manager.get_usb_port()
        if port:
            fw = f"v{self._detected_version}" if self._detected_version else "–"
            self.after(0, lambda: self._ds_set_connected("USB", port, fw))
            self._tray_set_status("connected")
        else:
            self.after(0, self._ds_set_disconnected)
            self._tray_set_status("idle")
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        tk.Button(dlg, text="  Close  ", font=F_BODY,
                  bg=DANGER, fg=WHITE, activebackground="#A0291E",
                  relief="flat", bd=0, padx=16, pady=8,
                  command=dlg.destroy).pack(pady=(10, 14))

    # ── System Tray ───────────────────────────────────────────────────────
    def _start_tray(self):
        """Create and run the system tray icon in a background thread."""
        if not _TRAY_AVAILABLE:
            return

        def _build_menu():
            # Status line (not clickable)
            status_text = self._tray_status_text()
            return pystray.Menu(
                pystray.MenuItem(f"AlphaSense  ·  {status_text}",
                                 None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Show Dashboard",   self._tray_show,
                                 default=True),
                pystray.MenuItem("Hide to Tray",     self._tray_hide),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Check for Update", self._tray_check_update),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit AlphaSense",  self._tray_exit),
            )

        img = _make_tray_image(_TRAY_COLOR["idle"])
        self._tray_icon = pystray.Icon(
            name    = "AlphaSense",
            icon    = img,
            title   = "AlphaSense — No device connected",
            menu    = pystray.Menu(
                pystray.MenuItem("Show Dashboard",   self._tray_show,
                                 default=True),
                pystray.MenuItem("Hide to Tray",     self._tray_hide),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Check for Update", self._tray_check_update),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit AlphaSense",  self._tray_exit),
            ),
        )

        # Run tray in its own daemon thread so it doesn't block tkinter
        threading.Thread(
            target=self._tray_icon.run,
            daemon=True,
            name="TrayThread"
        ).start()

    def _tray_status_text(self) -> str:
        if self._tray_status == "connected":
            port = self._manager.get_usb_port()
            fw   = f"v{self._detected_version}" if self._detected_version else ""
            return f"Connected  {port or 'BLE'}  {fw}".strip()
        elif self._tray_status == "connecting":
            return "Connecting…"
        elif self._tray_status == "unauthorized":
            return "Unauthorized device"
        elif self._tray_status == "uploading":
            return "Firmware updating…"
        return "No device connected"

    def _tray_set_status(self, status: str):
        """Update tray icon colour and tooltip to reflect connection state."""
        if not _TRAY_AVAILABLE or self._tray_icon is None:
            return
        self._tray_status = status
        color   = _TRAY_COLOR.get(status, _TRAY_COLOR["idle"])
        tooltip = f"AlphaSense  ·  {self._tray_status_text()}"
        try:
            self._tray_icon.icon  = _make_tray_image(color)
            self._tray_icon.title = tooltip
        except Exception:
            pass

    def _tray_show(self, icon=None, item=None):
        """Restore the main window from tray."""
        self.after(0, self._restore_window)

    def _tray_hide(self, icon=None, item=None):
        """Hide main window to tray."""
        self.after(0, self._minimize_to_tray)

    def _tray_check_update(self, icon=None, item=None):
        """Trigger update check from tray menu."""
        def _run():
            if self._usb_connected:
                port = self._manager.get_usb_port()
                if port:
                    self.after(0, lambda: self._start_update(port))
            else:
                self.after(0, self._restore_window)
        threading.Thread(target=_run, daemon=True).start()

    def _tray_exit(self, icon=None, item=None):
        """Fully exit the application from the tray menu."""
        def _do_exit():
            if self._tray_icon:
                try:
                    self._tray_icon.stop()
                except Exception:
                    pass
            self._manager.stop()
            self.destroy()
        self.after(0, _do_exit)

    def _minimize_to_tray(self):
        """Hide the window — it keeps running in the tray."""
        self.withdraw()
        if not self._minimize_notified and _TRAY_AVAILABLE and self._tray_icon:
            self._minimize_notified = True
            # Show balloon tip (Windows only via pystray)
            try:
                self._tray_icon.notify(
                    "AlphaSense is still running",
                    "Right-click the tray icon to show or exit."
                )
            except Exception:
                pass

    def _restore_window(self):
        """Bring the window back from tray."""
        self.deiconify()
        self.lift()
        self.focus_force()

    # ── Uptime ────────────────────────────────────────────────────────────
    def _tick_uptime(self):
        e = int(time.time() - self._start_time)
        h, r = divmod(e, 3600)
        m, s = divmod(r, 60)
        self._uptime_lbl.config(text=f"Uptime:  {h:02d}:{m:02d}:{s:02d}")
        self.after(1000, self._tick_uptime)

    # ── Close (X button = full exit, tray icon also stops) ────────────────
    def _on_close(self):
        """Window X button → fully exit the application."""
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self._manager.stop()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT  — handle activation then launch dashboard
# ─────────────────────────────────────────────────────────────────────────────
def main():
    # Try to load saved license first
    product_key = load_license()

    if product_key is None:
        # First launch (or license deleted/tampered) — show activation dialog
        root = tk.Tk()
        root.withdraw()   # hide the root window, dialog is the only thing shown

        dlg = ProductKeyDialog(root)
        root.wait_window(dlg)

        product_key = dlg.result
        if not product_key:
            # User somehow bypassed (shouldn't happen) — just exit
            root.destroy()
            return

        root.destroy()

    # Launch the main dashboard with the verified key
    app = Dashboard(product_key=product_key)
    app.mainloop()


if __name__ == "__main__":
    main()
