/*
  ESP32 — Combined USB + BLE
  ───────────────────────────
  - USB Serial : always active, sends greeting on boot
  - BLE (NUS)  : advertises as "ESP32_BLE", sends greeting on connect

  Both run simultaneously — dashboard detects whichever is connected.

  Board   : ESP32 Dev Module
  No extra libraries needed (BLE is built into ESP32 Arduino core)
*/

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ── CONFIG ────────────────────────────────────
#define DEVICE_NAME  "ESP32_BLE"
#define USB_BAUD     115200

// Nordic UART Service UUIDs (must match dashboard)
#define NUS_SERVICE_UUID "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define NUS_RX_UUID      "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
#define NUS_TX_UUID      "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

// ── GLOBALS ───────────────────────────────────
BLECharacteristic* pTxChar   = nullptr;
bool               bleConnected = false;

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
      Serial.print("[BLE-RX] ");
      Serial.println(val);
    }
  }
};

// ── HELPERS ───────────────────────────────────
void bleSend(const String& msg) {
  if (bleConnected && pTxChar) {
    pTxChar->setValue(msg.c_str());
    pTxChar->notify();
  }
}

// ── SETUP ─────────────────────────────────────
void setup() {
  // USB Serial
  Serial.begin(USB_BAUD);
  delay(1000);
  Serial.println("hi this is esp32 connected using usb");

  // BLE setup
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
  Serial.println("BLE advertising as ESP32_BLE...");
}

// ── LOOP ──────────────────────────────────────
void loop() {
  static bool    bleGreeted = false;
  static unsigned long lastHB = 0;

  // BLE greeting on first connect
  if (bleConnected && !bleGreeted) {
    delay(300);
    bleSend("hi this is esp32 connected using bluetooth");
    bleGreeted = true;
  }
  if (!bleConnected) bleGreeted = false;

  // Heartbeat every 10s on both channels
  if (millis() - lastHB > 10000) {
    lastHB = millis();
    Serial.println("heartbeat: esp32 usb alive");
    bleSend("heartbeat: esp32 ble alive");
  }

  // Echo USB input back
  if (Serial.available()) {
    String msg = Serial.readStringUntil('\n');
    msg.trim();
    Serial.print("echo: ");
    Serial.println(msg);
  }

  delay(100);
}
