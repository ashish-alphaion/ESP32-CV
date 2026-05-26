/*
  ESP32 — Bluetooth (Classic RFCOMM) Connection
  ───────────────────────────────────────────────
  Advertises itself as "ESP32_BT", waits for the desktop
  dashboard to connect, then sends a greeting.

  Requirements:
    Board   : ESP32 Dev Module (classic BT, NOT ESP32-S2/C3 – those lack Classic BT)
    Library : Built-in BluetoothSerial (comes with ESP32 Arduino core)
*/

#include "BluetoothSerial.h"

BluetoothSerial SerialBT;

void setup() {
  Serial.begin(115200);

  // "ESP32_BT" must match ESP32_BT_NAME in the Python dashboard
  SerialBT.begin("ESP32_BT");
  Serial.println("Bluetooth started, waiting for connection...");
}

void loop() {
  // As soon as a client connects, send the greeting once
  static bool greeted = false;
  if (SerialBT.connected()) {
    if (!greeted) {
      SerialBT.println("hi this is esp32 connected using bluetooth");
      greeted = true;
    }

    // Echo anything received from the host
    if (SerialBT.available()) {
      String msg = SerialBT.readStringUntil('\n');
      msg.trim();
      SerialBT.print("echo: ");
      SerialBT.println(msg);
    }

    // Periodic heartbeat (every 10 s)
    static unsigned long last = 0;
    if (millis() - last > 10000) {
      last = millis();
      SerialBT.println("heartbeat: esp32 bt alive");
    }
  } else {
    greeted = false;   // reset so greeting fires again on reconnect
  }
}
