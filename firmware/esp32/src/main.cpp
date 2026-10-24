#include <Arduino.h>

#ifndef DEMO_BUG
#define DEMO_BUG 1
#endif

#define APERTURE_STR_(x) #x
#define APERTURE_STR(x) APERTURE_STR_(x)
// Kept in the image so the harness can identify which build was flashed.
static const char BUILD_TAG[] __attribute__((used)) =
    "APERTURE_BUILD esp32_safety DEMO_BUG=" APERTURE_STR(DEMO_BUG);

constexpr int TRIGGER = 4, PWM = 5, FAULT = 18, ACTUATOR = 19, SAFETY = 21;

void setup() {
  // UART0 (TX0/RX0) carries both esptool flashing from the Pi and runtime telemetry.
  Serial.begin(115200);
  Serial.printf("{\"boot\":\"%s\"}\n", BUILD_TAG);
  pinMode(TRIGGER, INPUT_PULLDOWN);
  pinMode(PWM, INPUT_PULLDOWN);
  pinMode(FAULT, INPUT_PULLDOWN);
  pinMode(ACTUATOR, OUTPUT);
  pinMode(SAFETY, OUTPUT);
  digitalWrite(ACTUATOR, LOW);
  digitalWrite(SAFETY, HIGH);
}

void loop() {
  // Harness PWM is 100Hz; pulseIn returns zero for constant HIGH/LOW.
  unsigned long highUs = pulseIn(PWM, HIGH, 25000);
  float duty = highUs ? min(1.0f, highUs / 10000.0f) : (digitalRead(PWM) ? 1.0f : 0.0f);
  int trig = digitalRead(TRIGGER);
  int fault = digitalRead(FAULT);
  const char *state = "IDLE";
  int actuator = 0;
  int safety = !fault;
  if (fault) {
    state = "FAULT_SHUTDOWN";
  } else if (trig && duty > 0.8f) {
    state = "THROTTLING";
  } else if (trig) {
    state = "ACTIVE_DRIVE";
    actuator = 1;
  }
  digitalWrite(ACTUATOR, actuator);
  if (!(DEMO_BUG && fault && (trig || duty > 0.8f))) {
    digitalWrite(SAFETY, safety);
  }
  Serial.printf("{\"state\":\"%s\",\"trig\":%d,\"fault\":%d,\"pwm\":%.3f,"
                "\"pwm_us\":%lu,\"actuator\":%d,\"safety\":%d}\n",
                state, trig, fault, duty, highUs, actuator, safety);
  delay(50);
}
