// Aperture OS rig-01 DUT: ESP32 DevKit V1 with a 16x2 LCD (4-bit) and a status LED.
// UART0 (115200) is shared with flashing from the Pi; one command per line:
//   LCD <line1>|<line2>  -> "OK LCD"        LED BLINK <hz> -> "OK LED <hz>"
//   LED ON / LED OFF     -> "OK LED ON" / "OK LED OFF"
// The safety-controller test logic on D4/D5/D18 -> D19/D21 is unchanged.
#include <Arduino.h>
#include <LiquidCrystal.h>

#ifndef DEMO_BUG
#define DEMO_BUG 1
#endif

constexpr int TRIGGER = 4, PWM_IN = 5, FAULT = 18, ACTUATOR = 19, SAFETY = 21;
constexpr int STATUS_LED = 13;
constexpr uint32_t BAUD = 115200, TELEMETRY_MS = 200;
constexpr size_t COMMAND_MAX = 64;

LiquidCrystal lcd(14, 27, 26, 25, 33, 32);  // RS, E, DB4, DB5, DB6, DB7

enum class LedMode { Blink, On, Off };
LedMode ledMode = LedMode::Blink;
float blinkHz = 2.0f;
String line;
uint32_t lastTelemetry = 0;

void showLcd(const String &top, const String &bottom) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(top.substring(0, 16));
  lcd.setCursor(0, 1);
  lcd.print(bottom.substring(0, 16));
}

void updateLed() {
  if (ledMode != LedMode::Blink) {
    digitalWrite(STATUS_LED, ledMode == LedMode::On ? HIGH : LOW);
    return;
  }
  uint32_t halfPeriodMs = blinkHz > 0 ? (uint32_t)(500.0f / blinkHz) : 0;
  digitalWrite(STATUS_LED, halfPeriodMs && (millis() / halfPeriodMs) % 2 ? HIGH : LOW);
}

void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.startsWith("LCD ")) {
    String text = cmd.substring(4);
    int bar = text.indexOf('|');
    showLcd(bar < 0 ? text : text.substring(0, bar), bar < 0 ? "" : text.substring(bar + 1));
    Serial.println("OK LCD");
  } else if (cmd == "LED ON") {
    ledMode = LedMode::On;
    Serial.println("OK LED ON");
  } else if (cmd == "LED OFF") {
    ledMode = LedMode::Off;
    Serial.println("OK LED OFF");
  } else if (cmd.startsWith("LED BLINK ")) {
    float hz = cmd.substring(10).toFloat();
    if (hz <= 0 || hz > 50) {
      Serial.println("ERR LED hz must be 0..50");
      return;
    }
    blinkHz = hz;
    ledMode = LedMode::Blink;
    Serial.print("OK LED ");
    Serial.println(cmd.substring(10));
  } else if (cmd.length()) {
    Serial.println("ERR unknown command");
  }
}

void readCommands() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (line.length()) handleCommand(line);
      line = "";
    } else if (line.length() < COMMAND_MAX) {
      line += c;
    }
  }
}

void safetyController() {
  // Pi drives trigger/PWM/fault; the Pi reads actuator and safety back on GPIO22/23.
  unsigned long highUs = pulseIn(PWM_IN, HIGH, 25000);
  float duty = highUs ? min(1.0f, highUs / 10000.0f) : (digitalRead(PWM_IN) ? 1.0f : 0.0f);
  int trig = digitalRead(TRIGGER), fault = digitalRead(FAULT);
  const char *state = fault ? "FAULT_SHUTDOWN" : (trig && duty > 0.8f) ? "THROTTLING" : trig ? "ACTIVE_DRIVE" : "IDLE";
  int actuator = (!fault && trig && duty <= 0.8f) ? 1 : 0;
  int safety = !fault;
  digitalWrite(ACTUATOR, actuator);
  if (!(DEMO_BUG && fault && (trig || duty > 0.8f))) digitalWrite(SAFETY, safety);
  if (millis() - lastTelemetry >= TELEMETRY_MS) {
    lastTelemetry = millis();
    Serial.printf("{\"state\":\"%s\",\"trig\":%d,\"fault\":%d,\"pwm\":%.3f,\"actuator\":%d,\"safety\":%d}\n",
                  state, trig, fault, duty, actuator, safety);
  }
}

void setup() {
  Serial.begin(BAUD);
  pinMode(TRIGGER, INPUT_PULLDOWN);
  pinMode(PWM_IN, INPUT_PULLDOWN);
  pinMode(FAULT, INPUT_PULLDOWN);
  pinMode(ACTUATOR, OUTPUT);
  pinMode(SAFETY, OUTPUT);
  pinMode(STATUS_LED, OUTPUT);
  digitalWrite(ACTUATOR, LOW);
  digitalWrite(SAFETY, HIGH);
  lcd.begin(16, 2);
  showLcd("Aperture OS", "rig-01 online");
  Serial.println("BOOT OK");
}

void loop() {
  readCommands();
  updateLed();
  safetyController();
}
