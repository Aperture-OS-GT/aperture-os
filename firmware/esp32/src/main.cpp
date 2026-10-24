#include <Arduino.h>

#ifndef DEMO_BUG
#define DEMO_BUG 1
#endif

constexpr int TRIGGER = 4, PWM = 5, FAULT = 18, ACTUATOR = 19, SAFETY = 21;

void setup() {
  Serial.begin(115200);
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
