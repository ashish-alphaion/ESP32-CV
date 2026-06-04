/*
  AlphaSense — USB (Serial) Connection
  ──────────────────────────────────────
  Security: HMAC-SHA256 challenge-response authentication.
  Device secret is XOR-obfuscated — not plain text in flash.
  Input length is capped to prevent stack overflow.

  Board : AlphaSense (ESP32 Dev Module)
  Speed : 115200
*/

#include "mbedtls/md.h"

// ── OBFUSCATED SECRET ─────────────────────────────────────────────────────
// Encodes "AS-K1AB-2C3D-4E5F-6G7H" XOR 0x5A  (change before flashing each unit)
static const uint8_t SECRET_ENC[] = {
  0x1B,0x19,0x77,0x3B,0x6B,0x1B,0x77,0x19,0x1E,0x77,0x79,0x6E,0x77,0x1E,0x1B,
  0x77,0x6A,0x2D,0x77,0x7C,0x3D,0x7B,0x32
};
static const size_t SECRET_LEN = sizeof(SECRET_ENC);

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
  if (raw.length() > MAX_CMD_LEN) return;   // drop oversized input

  String cmd = raw;
  cmd.trim();

  if (cmd == "VERSION?") {
    Serial.println("VERSION:1.0USB");

  } else if (cmd.startsWith("AUTH_CHALLENGE:")) {
    String nonce = cmd.substring(15);
    nonce.trim();
    if (nonce.length() == 0 || nonce.length() > 64) return;
    char secret[64];
    _get_secret(secret, sizeof(secret));
    String response = "AUTH_RESPONSE:" + hmacSHA256(secret, nonce);
    memset(secret, 0, sizeof(secret));   // zero secret from stack immediately
    Serial.println(response);

  }
  // All other commands are silently ignored — no arbitrary echo
}

// ── SETUP ─────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("hi this is AlphaSense connected using usb");
}

// ── LOOP ──────────────────────────────────────────────────────────────────
void loop() {
  if (Serial.available()) {
    String msg = Serial.readStringUntil('\n');
    if (msg.length() <= MAX_CMD_LEN) {
      msg.trim();
      handleCommand(msg);
    }
    // Oversized messages are silently dropped
  }

  static unsigned long last = 0;
  if (millis() - last > 10000) {
    last = millis();
    Serial.println("heartbeat: AlphaSense usb alive");
  }
}
