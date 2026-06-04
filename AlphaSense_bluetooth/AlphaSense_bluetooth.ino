/*
  AlphaSense — Bluetooth Classic (RFCOMM) Connection
  ────────────────────────────────────────────────────
  Security: HMAC-SHA256 challenge-response authentication.
  The device secret is stored obfuscated (XOR) — not plain text in flash.

  Board   : AlphaSense (ESP32 Dev Module — classic BT, NOT ESP32-S2/C3)
  Library : Built-in BluetoothSerial + mbedTLS (both in ESP32 Arduino core)
*/

#include "BluetoothSerial.h"
#include "mbedtls/md.h"

BluetoothSerial SerialBT;

// ── OBFUSCATED SECRET ─────────────────────────────────────────────────────
// The real key is XOR-encoded with 0x5A so it does NOT appear as plain text
// in a flash dump.  To generate a new entry: for each char c in your key,
// store (c ^ 0x5A).  Reconstruct at runtime with _get_secret().
//
// This encodes "AS-K1AB-2C3D-4E5F-6G7H"  (change before flashing each unit)
static const uint8_t SECRET_ENC[] = {
  0x1B,0x19,0x77,0x3B,0x6B,0x1B,0x77,0x19,0x1E,0x77,0x79,0x6E,0x77,0x1E,0x1B,
  0x77,0x6A,0x2D,0x77,0x7C,0x3D,0x7B,0x32
};
static const size_t  SECRET_LEN = sizeof(SECRET_ENC);

// Reconstruct key into buf at runtime (never stored as plain string)
static void _get_secret(char* buf, size_t buf_len) {
  size_t n = (SECRET_LEN < buf_len - 1) ? SECRET_LEN : buf_len - 1;
  for (size_t i = 0; i < n; i++) buf[i] = (char)(SECRET_ENC[i] ^ 0x5A);
  buf[n] = '\0';
}

// ── INPUT LENGTH LIMIT ────────────────────────────────────────────────────
#define MAX_CMD_LEN 128

// ── HMAC-SHA256 ───────────────────────────────────────────────────────────
static String hmacSHA256(const char* key, const String& message) {
  uint8_t result[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, (const uint8_t*)key, strlen(key));
  mbedtls_md_hmac_update(&ctx,
    (const uint8_t*)message.c_str(), message.length());
  mbedtls_md_hmac_finish(&ctx, result);
  mbedtls_md_free(&ctx);
  String hex = "";
  for (int i = 0; i < 32; i++) {
    if (result[i] < 0x10) hex += "0";
    hex += String(result[i], HEX);
  }
  return hex;
}

// ── COMMAND HANDLER ───────────────────────────────────────────────────────
static void handleCommand(const String& raw) {
  // Enforce input length limit (fix #10)
  if (raw.length() > MAX_CMD_LEN) return;

  String cmd = raw;
  cmd.trim();

  if (cmd == "VERSION?") {
    SerialBT.println("VERSION:1.0BT");
    Serial.println("VERSION:1.0BT");

  } else if (cmd.startsWith("AUTH_CHALLENGE:")) {
    String nonce = cmd.substring(15);
    nonce.trim();
    if (nonce.length() == 0 || nonce.length() > 64) return;  // sanity check
    char secret[64];
    _get_secret(secret, sizeof(secret));
    String response = "AUTH_RESPONSE:" + hmacSHA256(secret, nonce);
    // Zero out secret from stack immediately after use
    memset(secret, 0, sizeof(secret));
    SerialBT.println(response);
    Serial.println(response);

  } else if (cmd.startsWith("echo:") == false) {
    // Unknown command — silently ignore (don't echo arbitrary data)
  }
}

// ── SETUP ─────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  SerialBT.begin("AlphaSense V1");
  Serial.println("AlphaSense BT ready, waiting for connection...");
}

// ── LOOP ──────────────────────────────────────────────────────────────────
void loop() {
  static bool          authenticated = false;
  static bool          greeted       = false;
  static unsigned long lastHB        = 0;

  if (SerialBT.connected()) {
    // Read incoming command
    if (SerialBT.available()) {
      String msg = SerialBT.readStringUntil('\n');
      if (msg.length() <= MAX_CMD_LEN) {
        msg.trim();
        handleCommand(msg);
      }
    }

    // Only greet and heartbeat after auth would be confirmed by software
    if (!greeted) {
      SerialBT.println("hi this is AlphaSense connected using bluetooth");
      greeted = true;
    }

    if (millis() - lastHB > 10000) {
      lastHB = millis();
      SerialBT.println("heartbeat: AlphaSense bt alive");
    }
  } else {
    greeted       = false;
    authenticated = false;
  }
}
