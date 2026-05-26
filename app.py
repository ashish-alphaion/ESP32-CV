"""
ESP32 Connection Dashboard v4
Rules:
  - USB takes priority over BLE
  - If USB connects → BLE is paused/disconnected
  - If USB disconnects → BLE scanning resumes
  - Both can be off, but never both on simultaneously
"""

import tkinter as tk
import threading
import asyncio
import serial
import serial.tools.list_ports
from bleak import BleakScanner, BleakClient
import time

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
USB_BAUD_RATE   = 115200
USB_POLL_SEC    = 2
BLE_POLL_SEC    = 6
ESP32_USB_HINT  = "CP210"
BLE_DEVICE_NAME = "ESP32_BLE"
NUS_TX_UUID     = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

# ─────────────────────────────────────────────
# STYLE
# ─────────────────────────────────────────────
BG        = "#0d1117"
PANEL     = "#161b22"
ACCENT_G  = "#00e676"
ACCENT_B  = "#40c4ff"
ACCENT_W  = "#e6edf3"
DIM       = "#484f58"
FONT_MONO = ("Courier New", 10)
FONT_UI   = ("Segoe UI", 10)


# ─────────────────────────────────────────────
# DeviceManager
# ─────────────────────────────────────────────
class DeviceManager:
    def __init__(self, log_cb, status_cb):
        self.log    = log_cb
        self.status = status_cb

        # USB state
        self._usb_serial = None
        self._usb_port   = None
        self._usb_lock   = threading.Lock()
        self._usb_active = False          # True when USB is connected

        # BLE state
        self._ble_client    = None
        self._ble_connected = False
        self._ble_lock      = threading.Lock()

        self._running = False
        self._loop    = None

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
    # USB
    # ─────────────────────────────────────────
    def _usb_loop(self):
        while self._running:
            port = self._find_usb()

            with self._usb_lock:
                is_open = self._usb_serial is not None and self._usb_serial.is_open

            if port and not is_open:
                # USB just plugged in — kill BLE first
                self._force_ble_off()
                self._connect_usb(port)

            elif not port and is_open:
                # USB unplugged
                self.log("USB disconnected.", "dim")
                self.status("disconnected", None)
                self._disconnect_usb()

            elif is_open:
                # Liveness check
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
            with self._usb_lock:
                s = self._usb_serial
            if s is None or not s.is_open:
                break
            try:
                line = s.readline().decode("utf-8", errors="replace").strip()
                if line:
                    self.log(f"[ESP32-USB] {line}", "usb")
            except Exception:
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
            # Skip BLE entirely if USB is active
            if self._usb_active:
                await asyncio.sleep(BLE_POLL_SEC)
                continue

            with self._ble_lock:
                already = self._ble_connected

            if not already:
                device = await self._scan_ble()
                if device:
                    # Double-check USB didn't connect during scan
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

            # Hold until disconnected or USB takes over
            while self._running and client.is_connected and not self._usb_active:
                await asyncio.sleep(1)

            # USB took over — drop BLE
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
        """Instantly drop BLE when USB connects."""
        with self._ble_lock:
            client = self._ble_client
        if client and client.is_connected:
            # Schedule disconnect on the BLE event loop
            asyncio.run_coroutine_threadsafe(client.disconnect(), self._loop)
        self._mark_ble_disconnected()
        self.status(None, "disconnected")


# ─────────────────────────────────────────────
# Dashboard UI
# ─────────────────────────────────────────────
class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ESP32 Connection Monitor")
        self.geometry("720x520")
        self.configure(bg=BG)
        self.resizable(True, True)
        self._build_ui()
        self._manager = DeviceManager(
            log_cb    = self._log,
            status_cb = self._update_status,
        )
        self._manager.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        hdr = tk.Frame(self, bg=PANEL, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⬡  ESP32 Monitor", font=("Segoe UI", 14, "bold"),
                 bg=PANEL, fg=ACCENT_W).pack(side="left", padx=16)

        # Priority notice
        tk.Label(hdr, text="USB priority mode", font=("Segoe UI", 8),
                 bg=PANEL, fg=DIM).pack(side="right", padx=16)

        status_row = tk.Frame(self, bg=BG, pady=8)
        status_row.pack(fill="x", padx=16)
        self._usb_card = self._status_card(status_row, "USB",       ACCENT_G)
        self._bt_card  = self._status_card(status_row, "BLUETOOTH", ACCENT_B)
        self._usb_card.pack(side="left", padx=(0, 12))
        self._bt_card.pack(side="left")

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

        self._log_box.tag_config("usb", foreground=ACCENT_G)
        self._log_box.tag_config("bt",  foreground=ACCENT_B)
        self._log_box.tag_config("err", foreground="#ff5555")
        self._log_box.tag_config("dim", foreground=DIM)
        self._log_box.tag_config("sys", foreground="#ffd700")

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
        text = tk.Label(frame, text="Not connected", font=("Segoe UI", 9), bg=PANEL, fg=DIM)
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

    def _log(self, msg, tag=""):
        def _w():
            self._log_box.config(state="normal")
            ts = time.strftime("%H:%M:%S")
            self._log_box.insert("end", f"[{ts}] {msg}\n", tag)
            self._log_box.see("end")
            self._log_box.config(state="disabled")
        self.after(0, _w)

    def _update_status(self, usb_state, bt_state):
        def _a():
            if usb_state == "connected":      self._set_card(self._usb_card, True)
            elif usb_state == "disconnected": self._set_card(self._usb_card, False)
            if bt_state == "connected":       self._set_card(self._bt_card, True)
            elif bt_state == "disconnected":  self._set_card(self._bt_card, False)
        self.after(0, _a)

    def _clear_log(self):
        self._log_box.config(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.config(state="disabled")

    def _on_close(self):
        self._manager.stop()
        self.destroy()


if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()