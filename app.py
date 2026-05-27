"""
ESP32 Connection Dashboard v5
Rules:
  - USB takes priority over BLE
  - If USB connects → BLE is paused/disconnected
  - If USB disconnects → BLE scanning resumes
  - Both can be off, but never both on simultaneously
  - Version check: if ESP32 reports v1.0, user is offered an update to v2.0
  - Update is ONLY allowed over USB connection
"""

import tkinter as tk
from tkinter import messagebox
import threading
import asyncio
import serial
import serial.tools.list_ports
from bleak import BleakScanner, BleakClient
import time
import subprocess
import os
import sys

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
USB_BAUD_RATE   = 115200
USB_POLL_SEC    = 2
BLE_POLL_SEC    = 6
ESP32_USB_HINT  = "CP210"
BLE_DEVICE_NAME = "ESP32_BLE"
NUS_TX_UUID     = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

# Firmware version tracking
CURRENT_VERSION   = None        # version read from ESP32
LATEST_VERSION    = "2.0"       # version we can flash
UPDATE_AVAILABLE  = False       # set True when 1.0 is detected
UPDATE_NOTIFIED   = False       # so we don't spam the dialog

# Path to arduino-cli and the v2 sketch (relative to this script)
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
ARDUINO_CLI      = os.path.join(BASE_DIR,
                    "arduino-cli_1.5.0_Windows_64bit", "arduino-cli.exe")
SKETCH_V2_DIR    = os.path.join(BASE_DIR, "esp32_combined_v2")
FQBN             = "esp32:esp32:esp32"   # change if your board differs

# ─────────────────────────────────────────────
# STYLE
# ─────────────────────────────────────────────
BG        = "#0d1117"
PANEL     = "#161b22"
ACCENT_G  = "#00e676"
ACCENT_B  = "#40c4ff"
ACCENT_W  = "#e6edf3"
ACCENT_Y  = "#ffd700"
ACCENT_O  = "#ff9800"
DIM       = "#484f58"
FONT_MONO = ("Courier New", 10)
FONT_UI   = ("Segoe UI", 10)


# ─────────────────────────────────────────────
# DeviceManager
# ─────────────────────────────────────────────
class DeviceManager:
    def __init__(self, log_cb, status_cb, version_cb):
        self.log       = log_cb
        self.status    = status_cb
        self.version_cb = version_cb  # called when firmware version is parsed

        # USB state
        self._usb_serial  = None
        self._usb_port    = None
        self._usb_lock    = threading.Lock()
        self._usb_active  = False
        self._uploading   = False   # NEW: True while firmware upload is in progress

        # BLE state
        self._ble_client    = None
        self._ble_connected = False
        self._ble_lock      = threading.Lock()

        self._running = False
        self._loop    = None

    def get_usb_port(self):
        with self._usb_lock:
            return self._usb_port

    def start(self):
        self._running = True
        threading.Thread(target=self._usb_loop, daemon=True).start()
        threading.Thread(target=self._ble_thread, daemon=True).start()

    def stop(self):
        self._running = False
        self._disconnect_usb()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ─────────────────────────────────────────
    # NEW: Release port before upload, reclaim after
    # ─────────────────────────────────────────
    def release_port_for_upload(self):
        """Close the serial port so arduino-cli can use it for uploading."""
        with self._usb_lock:
            self._uploading = True
            try:
                if self._usb_serial and self._usb_serial.is_open:
                    self._usb_serial.close()
            except Exception:
                pass
            # Keep _usb_port so we can reconnect after upload

    def reclaim_port_after_upload(self):
        """Re-open the serial port after upload finishes."""
        with self._usb_lock:
            port = self._usb_port
            self._uploading = False
        # The _usb_loop will detect the closed serial and reconnect naturally
        # on its next iteration. No extra action needed.

    # ─────────────────────────────────────────
    # USB
    # ─────────────────────────────────────────
    def _usb_loop(self):
        while self._running:
            # Skip all USB management while upload is in progress
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
                self.log("USB disconnected.", "dim")
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
        for p in serial.tools.list_ports.comports():
            desc = (p.description or "") + (p.manufacturer or "")
            if ESP32_USB_HINT.lower() in desc.lower():
                return p.device
        return None

    def _connect_usb(self, port):
        try:
            s = serial.Serial(port, USB_BAUD_RATE, timeout=1)
            with self._usb_lock:
                self._usb_serial = s
                self._usb_port   = port
                self._usb_active = True
            self.log(f"ESP32 connected using USB on {port}", "usb")
            self.status("connected", None)
            threading.Thread(target=self._usb_read, daemon=True).start()
        except Exception as e:
            self.log(f"USB open error: {e}", "err")

    def _usb_read(self):
        while self._running:
            # Pause reading while upload is in progress
            if self._uploading:
                time.sleep(0.5)
                continue

            with self._usb_lock:
                s = self._usb_serial
            if s is None or not s.is_open:
                break
            try:
                line = s.readline().decode("utf-8", errors="replace").strip()
                if line:
                    # Check for VERSION: prefix — don't display it raw
                    if line.startswith("VERSION:"):
                        version = line.split(":", 1)[1].strip()
                        self.version_cb(version)
                    else:
                        self.log(f"[ESP32-USB] {line}", "usb")
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

    # ─────────────────────────────────────────
    # BLE
    # ─────────────────────────────────────────
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
            devices = await BleakScanner.discover(timeout=5)
            for d in devices:
                if d.name == BLE_DEVICE_NAME:
                    return d
        except Exception as e:
            self.log(f"BLE scan error: {e}", "err")
        return None

    async def _connect_ble(self, device):
        client = BleakClient(
            device.address,
            disconnected_callback=self._on_ble_disconnect
        )
        try:
            await client.connect()
            if not client.is_connected:
                return

            with self._ble_lock:
                self._ble_client    = client
                self._ble_connected = True

            self.log(f"ESP32 is connected using Bluetooth ({device.address})", "bt")
            self.status(None, "connected")
            await client.start_notify(NUS_TX_UUID, self._on_ble_data)

            while self._running and client.is_connected and not self._usb_active:
                await asyncio.sleep(1)

            if self._usb_active and client.is_connected:
                self.log("USB connected — Bluetooth paused.", "dim")
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
        if msg:
            self.log(f"[ESP32-BLE] {msg}", "bt")

    def _on_ble_disconnect(self, client):
        self.log("Bluetooth disconnected.", "dim")
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


# ─────────────────────────────────────────────
# OTA Upload Dialog
# ─────────────────────────────────────────────
class UpdateDialog(tk.Toplevel):
    """Shown when v1.0 is detected via USB — user can Update or Cancel."""

    def __init__(self, parent, port, on_update, on_cancel):
        super().__init__(parent)
        self.title("Firmware Update Available")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()                    # modal
        self.protocol("WM_DELETE_WINDOW", on_cancel)

        # Centre over parent
        self.geometry("480x320")
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - 480) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 320) // 2
        self.geometry(f"+{px}+{py}")

        tk.Label(self, text="⬆  Firmware Update Available",
                 font=("Segoe UI", 13, "bold"), bg=BG, fg=ACCENT_Y).pack(pady=(20, 6))

        tk.Label(self,
                 text="Your ESP32 is running  firmware v1.0\nA new version  v2.0  is available.",
                 font=("Segoe UI", 10), bg=BG, fg=ACCENT_W, justify="center").pack(pady=4)

        # Warning box
        warn = tk.Frame(self, bg="#2d2200", bd=1, relief="solid")
        warn.pack(fill="x", padx=30, pady=10)
        tk.Label(warn,
                 text="⚠  Before clicking Update:\n\n"
                      "  1. Make sure the USB cable is connected.\n"
                      "  2. Hold the  BOOT  button on your ESP32\n"
                      "     while the upload starts.\n"
                      "  3. Release BOOT once upload begins.",
                 font=("Segoe UI", 9), bg="#2d2200", fg=ACCENT_O,
                 justify="left").pack(padx=12, pady=10)

        tk.Label(self, text=f"Target port:  {port}",
                 font=("Courier New", 9), bg=BG, fg=DIM).pack(pady=(0, 6))

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(pady=10)

        tk.Button(btn_row, text="  ✖  Cancel  ", font=("Segoe UI", 10),
                  bg=PANEL, fg=DIM, activebackground=BG, activeforeground=ACCENT_W,
                  bd=0, relief="flat", padx=8, pady=6,
                  command=on_cancel).pack(side="left", padx=12)

        tk.Button(btn_row, text="  ⬆  Update to v2.0  ", font=("Segoe UI", 10, "bold"),
                  bg=ACCENT_G, fg="#000000", activebackground="#00c853",
                  bd=0, relief="flat", padx=8, pady=6,
                  command=on_update).pack(side="left", padx=12)


# ─────────────────────────────────────────────
# Upload Progress Dialog
# ─────────────────────────────────────────────
class UploadDialog(tk.Toplevel):
    """Shows live upload output while arduino-cli is running."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Uploading Firmware v2.0…")
        self.configure(bg=BG)
        self.resizable(True, False)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # block close during upload

        self.geometry("560x320")
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - 560) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 320) // 2
        self.geometry(f"+{px}+{py}")

        tk.Label(self, text="⬆  Uploading Firmware v2.0 — Please Wait…",
                 font=("Segoe UI", 11, "bold"), bg=BG, fg=ACCENT_Y).pack(pady=(16, 4))

        tk.Label(self, text="👉  Hold the BOOT button on your ESP32 until the upload starts!",
                 font=("Segoe UI", 9), bg=BG, fg=ACCENT_O).pack(pady=(0, 8))

        log_frame = tk.Frame(self, bg=PANEL)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        self._txt = tk.Text(log_frame, bg=BG, fg=ACCENT_W, font=("Courier New", 9),
                            bd=0, relief="flat", wrap="word", state="disabled", height=10)
        sb = tk.Scrollbar(log_frame, command=self._txt.yview)
        self._txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._txt.pack(fill="both", expand=True, padx=6, pady=6)

        self._status_lbl = tk.Label(self, text="Compiling sketch…",
                                    font=("Segoe UI", 9), bg=BG, fg=DIM)
        self._status_lbl.pack(pady=(0, 10))

    def append(self, line):
        self._txt.config(state="normal")
        self._txt.insert("end", line + "\n")
        self._txt.see("end")
        self._txt.config(state="disabled")

    def set_status(self, msg, color=DIM):
        self._status_lbl.config(text=msg, fg=color)


# ─────────────────────────────────────────────
# Dashboard UI
# ─────────────────────────────────────────────
class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__(  )
        self.title("ESP32 Connection Monitor")
        self.geometry("720x560")
        self.configure(bg=BG)
        self.resizable(True, True)

        self._detected_version = None
        self._update_notified  = False
        self._usb_connected    = False

        self._build_ui()
        self._manager = DeviceManager(
            log_cb      = self._log,
            status_cb   = self._update_status,
            version_cb  = self._on_version_received,
        )
        self._manager.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─────────────────────────────────────────
    # UI Build
    # ─────────────────────────────────────────
    def _build_ui(self):
        hdr = tk.Frame(self, bg=PANEL, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⬡  ESP32 Monitor", font=("Segoe UI", 14, "bold"),
                 bg=PANEL, fg=ACCENT_W).pack(side="left", padx=16)
        tk.Label(hdr, text="USB priority mode", font=("Segoe UI", 8),
                 bg=PANEL, fg=DIM).pack(side="right", padx=16)

        # ── Version bar ──
        self._ver_bar = tk.Frame(self, bg="#0f2027", pady=6)
        self._ver_bar.pack(fill="x", padx=16, pady=(6, 0))
        self._ver_lbl = tk.Label(self._ver_bar, text="Firmware: unknown",
                                 font=("Segoe UI", 9), bg="#0f2027", fg=DIM)
        self._ver_lbl.pack(side="left", padx=12)
        self._upd_btn = tk.Button(self._ver_bar, text="⬆ Update Available",
                                  font=("Segoe UI", 9, "bold"),
                                  bg=ACCENT_Y, fg="#000000",
                                  activebackground="#e6c200",
                                  bd=0, relief="flat", padx=8, pady=3,
                                  command=self._show_update_dialog)
        # Don't pack yet — shown only when update is available

        # ── Status cards ──
        status_row = tk.Frame(self, bg=BG, pady=8)
        status_row.pack(fill="x", padx=16)
        self._usb_card = self._status_card(status_row, "USB",       ACCENT_G)
        self._bt_card  = self._status_card(status_row, "BLUETOOTH", ACCENT_B)
        self._usb_card.pack(side="left", padx=(0, 12))
        self._bt_card.pack(side="left")

        # ── Log ──
        log_frame = tk.Frame(self, bg=PANEL)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        tk.Label(log_frame, text=" Message Log", font=("Segoe UI", 9, "bold"),
                 bg=PANEL, fg=DIM, anchor="w").pack(fill="x", padx=8, pady=(6, 0))

        self._log_box = tk.Text(
            log_frame, bg=BG, fg=ACCENT_W, font=FONT_MONO,
            bd=0, relief="flat", wrap="word", state="disabled",
        )
        sb = tk.Scrollbar(log_frame, command=self._log_box.yview)
        self._log_box.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_box.pack(fill="both", expand=True, padx=8, pady=8)

        self._log_box.tag_config("usb",  foreground=ACCENT_G)
        self._log_box.tag_config("bt",   foreground=ACCENT_B)
        self._log_box.tag_config("err",  foreground="#ff5555")
        self._log_box.tag_config("dim",  foreground=DIM)
        self._log_box.tag_config("sys",  foreground=ACCENT_Y)
        self._log_box.tag_config("upd",  foreground=ACCENT_O)

        tk.Button(self, text="Clear Log", bg=PANEL, fg=DIM,
                  activebackground=BG, activeforeground=ACCENT_W,
                  bd=0, relief="flat", font=FONT_UI,
                  command=self._clear_log).pack(anchor="e", padx=16, pady=(0, 10))

        self._log("System ready. USB takes priority over Bluetooth.", "sys")

    def _status_card(self, parent, label, color):
        frame = tk.Frame(parent, bg=PANEL, padx=14, pady=10)
        tk.Label(frame, text=label, font=("Segoe UI", 8, "bold"),
                 bg=PANEL, fg=DIM).pack(anchor="w")
        dot  = tk.Label(frame, text="●", font=("Segoe UI", 18), bg=PANEL, fg=DIM)
        dot.pack()
        text = tk.Label(frame, text="Not connected", font=("Segoe UI", 9),
                        bg=PANEL, fg=DIM)
        text.pack()
        frame._dot = dot; frame._text = text; frame._color = color
        return frame

    def _set_card(self, card, connected: bool):
        if connected:
            card._dot.config(fg=card._color)
            card._text.config(fg=card._color, text="Connected")
        else:
            card._dot.config(fg=DIM)
            card._text.config(fg=DIM, text="Not connected")

    # ─────────────────────────────────────────
    # Logging
    # ─────────────────────────────────────────
    def _log(self, msg, tag=""):
        def _w():
            self._log_box.config(state="normal")
            ts = time.strftime("%H:%M:%S")
            self._log_box.insert("end", f"[{ts}] {msg}\n", tag)
            self._log_box.see("end")
            self._log_box.config(state="disabled")
        self.after(0, _w)

    def _clear_log(self):
        self._log_box.config(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.config(state="disabled")

    # ─────────────────────────────────────────
    # Status callbacks
    # ─────────────────────────────────────────
    def _update_status(self, usb_state, bt_state):
        def _a():
            if usb_state == "connected":
                self._set_card(self._usb_card, True)
                self._usb_connected = True
            elif usb_state == "disconnected":
                self._set_card(self._usb_card, False)
                self._usb_connected = False
                # Hide update button if USB drops
                self._upd_btn.pack_forget()
                # Reset so we notify again on reconnect
                self._update_notified  = False
                self._detected_version = None
                self._ver_lbl.config(text="Firmware: unknown", fg=DIM)
            if bt_state == "connected":
                self._set_card(self._bt_card, True)
            elif bt_state == "disconnected":
                self._set_card(self._bt_card, False)
        self.after(0, _a)

    # ─────────────────────────────────────────
    # Version handling
    # ─────────────────────────────────────────
    def _on_version_received(self, version: str):
        """Called by DeviceManager when it reads a VERSION:x.x line."""
        def _handle():
            self._detected_version = version
            self._ver_lbl.config(
                text=f"Firmware: v{version}",
                fg=ACCENT_G if version == LATEST_VERSION else ACCENT_O
            )
            self._log(f"ESP32 firmware version detected: v{version}", "sys")

            if version == "1.0" and not self._update_notified and self._usb_connected:
                self._update_notified = True
                # Show the button in the version bar
                self._upd_btn.pack(side="right", padx=12)
                # Auto-show the dialog after a short delay
                self.after(800, self._show_update_dialog)

        self.after(0, _handle)

    # ─────────────────────────────────────────
    # Update dialog
    # ─────────────────────────────────────────
    def _show_update_dialog(self):
        """Open the update/cancel dialog. Only works if USB is connected."""
        if not self._usb_connected:
            messagebox.showwarning(
                "USB Required",
                "Firmware update is only possible over USB.\n"
                "Please connect the ESP32 via USB cable and try again.",
                parent=self
            )
            return

        port = self._manager.get_usb_port()
        if not port:
            messagebox.showwarning(
                "No USB Port",
                "Cannot determine the USB port. Please reconnect the ESP32.",
                parent=self
            )
            return

        dlg = UpdateDialog(
            self,
            port,
            on_update = lambda: self._start_update(dlg, port),
            on_cancel = lambda: self._cancel_update(dlg),
        )

    def _cancel_update(self, dlg):
        dlg.destroy()
        self._log("Firmware update cancelled by user.", "dim")

    # ─────────────────────────────────────────
    # Perform OTA upload via arduino-cli
    # ─────────────────────────────────────────
    def _start_update(self, update_dlg, port):
        update_dlg.destroy()

        progress = UploadDialog(self)
        self._log("Starting firmware upload to v2.0…", "upd")

        def _do_upload():
            sketch = SKETCH_V2_DIR
            cli    = ARDUINO_CLI

            if not os.path.isfile(cli):
                msg = (f"arduino-cli not found at:\n{cli}\n\n"
                       "Please ensure the arduino-cli folder is next to app.py.")
                progress.after(0, lambda: self._upload_failed(progress, msg))
                return

            if not os.path.isdir(sketch):
                msg = (f"Sketch folder not found:\n{sketch}\n\n"
                       "Please ensure 'esp32_combined_v2' folder is next to app.py.")
                progress.after(0, lambda: self._upload_failed(progress, msg))
                return

            progress.after(0, lambda: progress.set_status("Compiling sketch…", ACCENT_Y))
            progress.after(0, lambda: progress.append("► Compiling esp32_combined_v2…"))

            # Step 1: compile
            compile_cmd = [
                cli, "compile",
                "--fqbn", FQBN,
                sketch,
                "--verbose",
            ]
            ok = self._run_cli(compile_cmd, progress)
            if not ok:
                self._manager.reclaim_port_after_upload()
                return

            # Step 2: release the serial port so arduino-cli can open it
            progress.after(0, lambda: progress.append(
                "\n► Releasing serial port for upload…"))
            self._manager.release_port_for_upload()
            time.sleep(0.5)   # brief pause to ensure the port is fully closed

            # Step 3: upload
            progress.after(0, lambda: progress.set_status(
                "Uploading — hold BOOT button now!", ACCENT_O))
            progress.after(0, lambda: progress.append(
                "\n👉  HOLD the BOOT button on your ESP32 NOW!\n"))

            upload_cmd = [
                cli, "upload",
                "--fqbn", FQBN,
                "--port", port,
                sketch,
                "--verbose",
            ]
            ok = self._run_cli(upload_cmd, progress)

            # Step 4: always reclaim the port after upload (success or fail)
            self._manager.reclaim_port_after_upload()

            if not ok:
                return

            progress.after(0, lambda: self._upload_success(progress))

        threading.Thread(target=_do_upload, daemon=True).start()

    def _run_cli(self, cmd, progress_dlg):
        """Run an arduino-cli command, streaming output to the upload dialog."""
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                line = line.rstrip()
                progress_dlg.after(0, lambda l=line: progress_dlg.append(l))
            proc.wait()
            if proc.returncode != 0:
                msg = f"Command failed (exit code {proc.returncode})"
                progress_dlg.after(0, lambda: self._upload_failed(progress_dlg, msg))
                return False
            return True
        except FileNotFoundError:
            msg = f"Cannot run:\n{' '.join(cmd)}\n\nCheck that arduino-cli.exe exists."
            progress_dlg.after(0, lambda: self._upload_failed(progress_dlg, msg))
            return False
        except Exception as e:
            progress_dlg.after(0, lambda: self._upload_failed(progress_dlg, str(e)))
            return False

    def _upload_success(self, progress_dlg):
        progress_dlg.set_status("✔ Upload complete! ESP32 is now running v2.0.", ACCENT_G)
        progress_dlg.append("\n✔ Firmware v2.0 uploaded successfully!")
        self._log("✔ Firmware updated to v2.0 successfully!", "sys")
        self._upd_btn.pack_forget()
        self._ver_lbl.config(text="Firmware: v2.0 (up to date)", fg=ACCENT_G)
        # Allow closing the progress dialog now
        progress_dlg.protocol("WM_DELETE_WINDOW", progress_dlg.destroy)
        tk.Button(progress_dlg, text="Close", bg=ACCENT_G, fg="#000000",
                  font=("Segoe UI", 10, "bold"), bd=0, relief="flat",
                  padx=12, pady=6,
                  command=progress_dlg.destroy).pack(pady=(0, 12))

    def _upload_failed(self, progress_dlg, reason):
        progress_dlg.set_status("✖ Upload failed.", "#ff5555")
        progress_dlg.append(f"\n✖ ERROR: {reason}")
        self._log(f"Firmware upload failed: {reason}", "err")
        progress_dlg.protocol("WM_DELETE_WINDOW", progress_dlg.destroy)
        tk.Button(progress_dlg, text="Close", bg="#ff5555", fg="#ffffff",
                  font=("Segoe UI", 10, "bold"), bd=0, relief="flat",
                  padx=12, pady=6,
                  command=progress_dlg.destroy).pack(pady=(0, 12))

    # ─────────────────────────────────────────
    # Close
    # ─────────────────────────────────────────
    def _on_close(self):
        self._manager.stop()
        self.destroy()


if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()
