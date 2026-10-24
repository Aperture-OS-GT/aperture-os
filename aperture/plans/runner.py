"""Deterministic plan runner: executes a plan's steps through the tool set, no agent in the loop."""
from __future__ import annotations

from ..tools import ToolError, Tools
from .schema import validate_plan

ASSERT_TOOLS = ("assert_pin", "assert_timeline", "assert_frequency")


class PlanRunner:
    def __init__(self, tools: Tools, plan: dict):
        self.tools = tools
        self.session = tools.session
        self.plan = validate_plan(plan)

    def preflight(self) -> dict:
        """Wiring the plan requires that the session has not confirmed (only when the policy is ``ask``)."""
        if self.tools._policy() == "off":
            return {"ok": True, "missing": []}
        missing = [w for w in self.plan["requires"]["wiring"] if w["pin"] not in self.session.wiring]
        return {"ok": not missing, "missing": missing}

    def run(self) -> dict:
        check = self.preflight()
        if not check["ok"]:
            self.session.record("plan.blocked", "system", plan=self.plan["slug"], missing=check["missing"])
            return {"pass": False, "blocked": check["missing"], "steps": [], "plan": self.plan["slug"]}
        self.session.record("plan.start", "system", plan=self.plan["slug"], name=self.plan["name"],
                            source=self.plan["source"], steps=len(self.plan["steps"]))
        results, failed, aborted = [], [], False
        try:
            for step in self.plan["steps"]:
                outcome = self._step(step)
                results.append(outcome)
                if not outcome["pass"]:
                    failed.append(step["id"])
                    if step.get("on_fail", "stop") == "stop":
                        aborted = True
                        break
        finally:
            for step in self.plan["cleanup"]:
                results.append(self._step(step, cleanup=True))
            transport = self.session.transport
            if transport and transport.connected and transport.control_token:
                self.session.request("release_all", actor="system")
        passed = not failed and not aborted
        self.session.record("plan.end", "system", plan=self.plan["slug"], **{"pass": passed},
                            failed_steps=failed, aborted=aborted)
        self.session.verdict = passed if self.session.verdict is None else (self.session.verdict and passed)
        return {"pass": passed, "failed_steps": failed, "aborted": aborted, "steps": results,
                "plan": self.plan["slug"]}

    def _step(self, step: dict, cleanup: bool = False) -> dict:
        before = self.session._req_n
        outcome = {"id": step["id"], "tool": step["tool"], "assert": bool(step.get("assert")), "cleanup": cleanup}
        try:
            result = self.tools.call(step["tool"], **step["args"])
            outcome["pass"] = bool(result.get("pass", True))
            outcome["result"] = result
        except ToolError as exc:
            outcome["pass"] = False
            outcome["error"] = {"code": exc.code, "msg": exc.msg, **exc.extra}
        outcome["requests"] = [f"r{n}" for n in range(before + 1, self.session._req_n + 1)]
        if step.get("assert") and step["tool"] not in ASSERT_TOOLS:
            self.session.assertion(step.get("name") or f"{step['id']} {step['tool']}", outcome["pass"],
                                   step["args"], outcome.get("result") or outcome.get("error"), "system")
        self.session.record("plan.step", "system", plan=self.plan["slug"], step=step["id"], tool=step["tool"],
                            **{"pass": outcome["pass"]}, cleanup=cleanup, requests=outcome["requests"],
                            error=outcome.get("error"))
        return outcome
