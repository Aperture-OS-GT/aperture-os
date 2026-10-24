"""Request lifecycle: requested -> acknowledged -> executed -> observed.

A ``Request`` is one agent/tester action. Each stage transition is recorded by
the session as a ``request.stage`` event; this module only holds the state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STAGES = ("requested", "acknowledged", "executed", "observed")


class LifecycleError(Exception):
    pass


@dataclass
class Request:
    id: str
    op: str
    args: dict
    actor: str
    stages: dict[str, dict] = field(default_factory=dict)
    ok: bool | None = None
    err: str | None = None
    result: dict = field(default_factory=dict)
    observation: dict | None = None

    @property
    def stage(self) -> str | None:
        for name in reversed(STAGES):
            if name in self.stages:
                return name
        return None

    def advance(self, stage: str, **detail) -> dict:
        if stage not in STAGES:
            raise LifecycleError(f"unknown stage {stage!r}")
        index = STAGES.index(stage)
        current = -1 if self.stage is None else STAGES.index(self.stage)
        if index <= current:
            raise LifecycleError(f"{self.id}: cannot go from {self.stage} back to {stage}")
        if index > current + 1 and not (stage == "executed" and current == 0 and detail.get("ack") == "implicit"):
            raise LifecycleError(f"{self.id}: cannot skip from {self.stage} to {stage}")
        self.stages[stage] = detail
        if stage == "executed":
            self.ok = detail.get("ok")
            self.err = detail.get("err")
            self.result = detail.get("result", {})
        if stage == "observed":
            self.observation = detail
        return detail

    def duration_us(self, start: str = "requested", end: str = "observed") -> int | None:
        a, b = self.stages.get(start), self.stages.get(end)
        if a is None or b is None:
            return None
        return b["t_host_us"] - a["t_host_us"]

    def as_dict(self) -> dict:
        return {"id": self.id, "op": self.op, "args": self.args, "actor": self.actor, "stage": self.stage,
                "ok": self.ok, "err": self.err, "result": self.result, "stages": self.stages}
