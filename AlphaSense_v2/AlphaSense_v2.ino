/*
  AlphaSense — Combined USB + BLE  [ VERSION 2.0 ]
  ─────────────────────────────────────────────────
  - USB Serial : always active, sends greeting on boot
  - BLE (NUS)  : advertises as "AlphaSense V2", sends greeting on connect
  - VERSION    : reported to dashboard so OTA update can be triggered
  - AUTH       : HMAC-SHA256 challenge-response passkey authentication

  Both run simultaneously — dashboard detects whichever is connected.

  Board   : AlphaSense (ESP32 Dev Module)
  No extra libraries needed (BLE + mbedTLS built into ESP32 Arduino core)
*/

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include "mbedtls/md.h"

// ── CONFIG ────────────────────────────────────
#define DEVICE_NAME   "AlphaSense V2"
#define USB_BAUD      115200
#define FW_VERSION    "2.0"

// ── SECRET PASSKEY ───────────────────────────────────────────────────────────
// Stored XOR-obfuscated (key = 0x5A) so it does NOT appear as plain text
// in a flash dump.  Each unit you ship gets a UNIQUE key here.
//
// To encode your key: for each character c, store (c ^ 0x5A).
// This encodes "AS-K1AB-2C3D-4E5F-6G7H" — CHANGE THIS PER UNIT.
static const uint8_t SECRET_ENC[] = {
  0x1B,0x19,0x77,0x3B,0x6B,0x1B,0x77,0x19,0x1E,0x77,0x79,0x6E,0x77,0x1E,0x1B,
  0x77,0x6A,0x2D,0x77,0x7C,0x3D,0x7B,0x32
};
static const size_t SECRET_LEN = sizeof(SECRET_ENC);

// Reconstruct key into buf at runtime — never held as a plain String
static void _get_secret(char* buf, size_t buf_len) {
  size_t n = (SECRET_LEN < buf_len - 1) ? SECRET_LEN : buf_len - 1;
  for (size_t i = 0; i < n; i++) buf[i] = (char)(SECRET_ENC[i] ^ 0x5A);
  buf[n] = '\0';
}

// ── INPUT LENGTH LIMIT ────────────────────────────────────────────────────
#define MAX_CMD_LEN 128

// Nordic UART Service UUIDs
#define NUS_SERVICE_UUID "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define NUS_RX_UUID      "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
#define NUS_TX_UUID      "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

// ── GLOBALS ───────────────────────────────────
BLECharacteristic* pTxChar    = nullptr;
bool               bleConnected = false;

// ── HMAC-SHA256 helper ────────────────────────
String hmacSHA256(const char* key, const String& message) {
  byte result[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx,
    (const unsigned char*)key, strlen(key));
  mbedtls_md_hmac_update(&ctx,
    (const unsigned char*)message.c_str(), message.length());
  mbedtls_md_hmac_finish(&ctx, result);
  mbedtls_md_free(&ctx);

  String hex = "";
  for (int i = 0; i < 32; i++) {
    if (result[i] < 0x10) hex += "0";
    hex += String(result[i], HEX);
  }
  return hex;
}

// ── BLE helpers (forward declaration — used in handleCommand) ────────────
void bleSend(const String& msg) {
  if (bleConnected && pTxChar) {
    pTxChar->setValue(msg.c_str());
    pTxChar->notify();
  }
}

// ── Handle incoming command ───────────────────
void handleCommand(const String& raw) {
  // Drop oversized input to prevent stack overflow (fix #10)
  if (raw.length() > MAX_CMD_LEN) return;

  String cmd = raw;
  cmd.trim();

  if (cmd == "VERSION?") {
    Serial.println("VERSION:" FW_VERSION);
    if (bleConnected) bleSend("VERSION:" FW_VERSION);

  } else if (cmd.startsWith("AUTH_CHALLENGE:")) {
    String nonce = cmd.substring(15);
    nonce.trim();
    if (nonce.length() == 0 || nonce.length() > 64) return;
    char secret[64];
    _get_secret(secret, sizeof(secret));
    String response = "AUTH_RESPONSE:" + hmacSHA256(secret, nonce);
    memset(secret, 0, sizeof(secret));   // zero secret from stack immediately
    Serial.println(response);
    if (bleConnected) bleSend(response);

  }
  // All other commands silently ignored
}

// ── BLE CALLBACKS ─────────────────────────────
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* s) override {
    bleConnected = true;
    Serial.println("BLE client connected");
  }
  void onDisconnect(BLEServer* s) override {
    bleConnected = false;
    Serial.println("BLE client disconnected, restarting advertising...");
    s->startAdvertising();
  }
};

class RxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    String val = c->getValue().c_str();
    if (val.length()) {
      handleCommand(val);
    }
  }
};

// ── SETUP ─────────────────────────────────────
void setup() {
  Serial.begin(USB_BAUD);
  delay(1000);

  Serial.println("VERSION:" FW_VERSION);
  Serial.println("[v2.0] AlphaSense is now running firmware version 2.0 — connected via USB.");

  BLEDevice::init(DEVICE_NAME);
  BLEServer* pServer = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());

  BLEService* pService = pServer->createService(NUS_SERVICE_UUID);

  pTxChar = pService->createCharacteristic(
    NUS_TX_UUID, BLECharacteristic::PROPERTY_NOTIFY
  );
  pTxChar->addDescriptor(new BLE2902());

  BLECharacteristic* pRxChar = pService->createCharacteristic(
    NUS_RX_UUID, BLECharacteristic::PROPERTY_WRITE
  );
  pRxChar->setCallbacks(new RxCallbacks());

  pService->start();
  pServer->getAdvertising()->start();
  Serial.println("BLE advertising as AlphaSense V2...");
}

// ── LOOP ──────────────────────────────────────
void loop() {
  static bool          bleGreeted = false;
  static unsigned long lastHB     = 0;

  if (bleConnected && !bleGreeted) {
    delay(300);
    bleSend("[v2.0] AlphaSense running firmware 2.0 — connected via Bluetooth. Upgrade successful!");
    bleGreeted = true;
  }
  if (!bleConnected) bleGreeted = false;

  if (millis() - lastHB > 10000) {
    lastHB = millis();
    Serial.println("[v2.0] heartbeat: AlphaSense usb alive — firmware 2.0");
    bleSend("[v2.0] heartbeat: AlphaSense ble alive — firmware 2.0");
  }

  // Handle USB commands with length guard
  if (Serial.available()) {
    String msg = Serial.readStringUntil('\n');
    if (msg.length() <= MAX_CMD_LEN) {
      handleCommand(msg);
    }
  }

  delay(100);
}
