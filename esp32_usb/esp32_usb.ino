/*
  ESP32 — USB (Serial) Connection
  ─────────────────────────────────
  Sends a greeting message over Serial (USB) once on boot,
  then echoes anything the host sends back.

  Upload via Arduino IDE:
    Board   : ESP32 Dev Module
    Speed   : 115200
*/

void setup() {
  Serial.begin(115200);
  delay(1000);                          // wait for Serial to settle

  // Greeting – the dashboard will display this line
  Serial.println("hi this is esp32 connected using usb");
}

void loop() {
  // Echo anything received from the host (optional / for testing)
  if (Serial.available()) {
    String msg = Serial.readStringUntil('\n');
    msg.trim();
    Serial.print("echo: ");
    Serial.println(msg);
  }

  // Periodic heartbeat (every 10 s) – useful for debugging
  static unsigned long last = 0;
  if (millis() - last > 10000) {
    last = millis();
    Serial.println("heartbeat: esp32 usb alive");
  }
}
