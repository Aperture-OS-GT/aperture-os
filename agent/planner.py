"""Deterministic verification is independent of LLM judgments."""
import json

SYSTEM_PROMPT = """You are Aperture OS, an Autonomous HIL Safety Verification Engine.
Generate a 4-step test plan (Nominal Operation, High Load PWM, Edge-Case Glitch,
Fault Recovery), execute each step on the physical rig, verify that the Pi's
physical loopback pins (GPIO 22/23) match the ESP32's reported Serial telemetry,
and flag any discrepancy. Include fault while trigger HIGH and fault above 80% PWM.
Use only the provided bounded hardware tools. Never trust Serial alone.
Do not claim measurements you have not made. Finish with a concise safety verdict.
"""


def verify(response):
    issues = []
    if not response.get("pass"):
        return [response.get("error", "harness failed")]
    try:
        for name, pin in (("actuator", "22"), ("safety", "23")):
            claimed = response["telemetry"][name]
            measured = response["physical"][pin]
            if claimed != measured:
                issues.append(f"DISCREPANCY: {name} Serial={claimed} physical GPIO{pin}={measured}")
        if response["injected"]["fault"]:
            if response["telemetry"]["state"] != "FAULT_SHUTDOWN":
                issues.append("fault did not enter FAULT_SHUTDOWN")
            if any(response["physical"][pin] != 0 for pin in ("22", "23")):
                issues.append("unsafe physical output during fault")
    except (KeyError, TypeError):
        issues.append("missing or malformed measurement")
    return issues


class Tools:
    def __init__(self, client):
        self.client = client
        self.observations = []

    def _record(self, response):
        result = {**response, "issues": verify(response)}
        self.observations.append(result)
        return result

    def stimulate_hardware(self, stimulus_type, value):
        if stimulus_type == "vector":
            response = self.client.command(cmd="run_vector", **value)
        elif stimulus_type == "digital":
            response = self.client.command(cmd="set_pin", pin=17, val=value)
        elif stimulus_type == "pwm":
            response = self.client.command(cmd="set_pwm", pin=18, duty=value)
        else:
            raise ValueError("stimulus_type must be vector, digital or pwm")
        return self._record(response)

    def read_hardware_state(self):
        return self._record(self.client.command(cmd="read_state"))

    def inject_hardware_fault(self, fault_pin=27, value=1):
        if fault_pin != 27:
            raise ValueError("only fault GPIO27 is wired")
        return self._record(self.client.command(cmd="inject_fault", val=value))


PLAN = [
    ("Nominal Operation", "Arm and drive under normal load", 1, 0.3, 0),
    ("High Load PWM", "Above 80% must throttle actuator", 1, 0.9, 0),
    ("Edge-Case Glitch", "Fault at high PWM with trigger LOW exposes relay race", 0, 0.9, 1),
    ("Fault Recovery", "Clear fault; verify re-armed idle", 0, 0, 0),
    ("Fault while trigger HIGH", "Fault must override active trigger", 1, 0.3, 1),
    ("Final Recovery", "Leave rig idle with trigger and PWM deasserted", 0, 0, 0),
]


def run_offline(client, display=None):
    tools = Tools(client)
    rows = []
    for name, reasoning, trig, pwm, fault in PLAN:
        result = tools.stimulate_hardware("vector", {"trig": trig, "pwm": pwm, "fault": fault})
        expected_state = {"Nominal Operation": "ACTIVE_DRIVE", "High Load PWM": "THROTTLING",
                          "Edge-Case Glitch": "FAULT_SHUTDOWN", "Fault Recovery": "IDLE",
                          "Fault while trigger HIGH": "FAULT_SHUTDOWN", "Final Recovery": "IDLE"}
        if result.get("telemetry", {}).get("state") != expected_state[name]:
            result["issues"].append(f"expected state {expected_state[name]}")
        row = {"step": name, "reasoning": reasoning, **result}
        rows.append(row)
        if display:
            display(row)
    return {"mode": "offline", "pass": not any(r["issues"] for r in rows), "steps": rows}


TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": name, "description": description,
     "parameters": {"type": "object", "properties": props, "required": required,
                    "additionalProperties": False}}}
    for name, description, props, required in [
        ("stimulate_hardware", "Inject a digital or PWM stimulus",
         {"stimulus_type": {"type": "string", "enum": ["digital", "pwm"]},
          "value": {"type": "number"}}, ["stimulus_type", "value"]),
        ("read_hardware_state", "Read serial and independent physical pins", {}, []),
        ("inject_hardware_fault", "Set or clear the hardware fault line",
         {"fault_pin": {"type": "integer", "enum": [27]},
          "value": {"type": "integer", "enum": [0, 1]}}, ["fault_pin", "value"]),
    ]
]


def run_llm(client, model, display=None, max_turns=30):
    from openai import OpenAI

    llm = OpenAI()
    tools = Tools(client)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Execute the safety test plan now."}]
    summary = "Turn limit reached; plan incomplete"
    complete = False
    try:
        for _ in range(max_turns):
            message = llm.chat.completions.create(
                model=model, messages=messages, tools=TOOL_SCHEMAS).choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                summary = message.content or ""
                complete = True
                break
            for call in message.tool_calls:
                functions = {name: getattr(tools, name) for name in (
                    "stimulate_hardware", "read_hardware_state", "inject_hardware_fault")}
                try:
                    result = functions[call.function.name](**json.loads(call.function.arguments))
                except (ValueError, TypeError, KeyError) as exc:
                    result = {"pass": False, "issues": [str(exc)], "latency_ms": 0}
                    tools.observations.append(result)
                if display:
                    display({"step": call.function.name, "reasoning": message.content or
                             "Execute bounded tool and independently verify", **result})
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result)})
    finally:
        tools.stimulate_hardware("vector", {"trig": 0, "pwm": 0, "fault": 0})
    return {"mode": "llm", "complete": complete, "summary": summary,
            "pass": complete and len(tools.observations) > 1 and
            not any(r["issues"] for r in tools.observations), "steps": tools.observations}
