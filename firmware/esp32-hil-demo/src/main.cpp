// Bench DUT for the reference HIL test: "confirm GPIO17 goes high within 50 ms of
// pressing the button on GPIO22; verify the ESP32 prints BOOT OK over UART".
// Mirrors the Virtual Rig scenario button_led so a plan that passes in CI exercises the
// same behaviour on real hardware. Pin numbers below are ESP32 GPIOs.
#include <Arduino.h>

constexpr int BUTTON = 4;      // <- Pi GPIO22, active-low (pull-up: released when Pi is high-Z)
constexpr int LED = 2;         // -> Pi GPIO17, HIGH while the button is pressed
constexpr int HEARTBEAT = 18;  // -> Pi GPIO24, free-running 2 Hz square wave
constexpr int UART_RX = 16, UART_TX = 17;  // <- Pi GPIO14 TXD0, -> Pi GPIO15 RXD0
constexpr uint32_t BAUD = 115200, BANNER_DELAY_MS = 300, HEARTBEAT_HALF_PERIOD_MS = 250;

void setup() {
  pinMode(BUTTON, INPUT_PULLUP);
  pinMode(LED, OUTPUT);
  pinMode(HEARTBEAT, OUTPUT);
  digitalWrite(LED, LOW);
  digitalWrite(HEARTBEAT, LOW);
  Serial2.begin(BAUD, SERIAL_8N1, UART_RX, UART_TX);
  delay(BANNER_DELAY_MS);
  Serial2.print("BOOT OK\n");
}

void loop() {
  digitalWrite(LED, digitalRead(BUTTON) == LOW ? HIGH : LOW);
  digitalWrite(HEARTBEAT, (millis() / HEARTBEAT_HALF_PERIOD_MS) % 2 ? HIGH : LOW);
  while (Serial2.available() > 0) {
    Serial2.write(static_cast<uint8_t>(Serial2.read()));  // byte-for-byte echo
  }
}
