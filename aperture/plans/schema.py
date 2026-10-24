"""Plan file format (schema 1), validation, and storage.

A plan is a deterministic, re-runnable list of tool calls plus assertions. Steps use
the same tool names and argument schemas the agent sees (``aperture.tools.SPECS``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..session import home_dir
from ..tools import ToolError, validate_args

SCHEMA = 1
EXAMPLES = Path(__file__).with_name("examples")
ON_FAIL = ("stop", "continue")


class PlanError(ValueError):
    def __init__(self, msg: str, step: str | None = None):
        super().__init__(f"{step}: {msg}" if step else msg)
        self.step = step


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise PlanError("plan name must contain letters or digits")
    return slug[:64]


def _steps(items, section: str) -> list[dict]:
    if not isinstance(items, list):
        raise PlanError(f"{section} must be a list")
    out, seen = [], set()
    for index, step in enumerate(items):
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
            raise PlanError(f"{section}[{index}] must be a map with a tool name")
        step_id = step.get("id") or f"{section[0]}{index + 1}"
        if not isinstance(step_id, str) or step_id in seen:
            raise PlanError("step ids must be unique strings", str(step_id))
        seen.add(step_id)
        args = step.get("args", {})
        if not isinstance(args, dict):
            raise PlanError("args must be a map", step_id)
        try:
            args = validate_args(step["tool"], args)
        except ToolError as exc:
            raise PlanError(exc.msg, step_id) from None
        on_fail = step.get("on_fail", "stop")
        if on_fail not in ON_FAIL:
            raise PlanError(f"on_fail must be one of {ON_FAIL}", step_id)
        timeout = step.get("timeout_ms")
        if timeout is not None and (type(timeout) is not int or timeout <= 0):
            raise PlanError("timeout_ms must be a positive integer", step_id)
        clean = {"id": step_id, "tool": step["tool"], "args": args}
        if step.get("assert"):
            clean["assert"] = True
        if on_fail != "stop":
            clean["on_fail"] = on_fail
        if timeout is not None:
            clean["timeout_ms"] = timeout
        if step.get("name"):
            clean["name"] = str(step["name"])
        out.append(clean)
    return out


def validate_plan(data: dict) -> dict:
    if not isinstance(data, dict):
        raise PlanError("plan must be a map")
    if data.get("schema", SCHEMA) != SCHEMA:
        raise PlanError(f"unsupported plan schema {data.get('schema')!r}")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PlanError("plan needs a name")
    requires = data.get("requires", {})
    if not isinstance(requires, dict):
        raise PlanError("requires must be a map")
    wiring = requires.get("wiring", [])
    for item in wiring:
        if not isinstance(item, dict) or type(item.get("pin")) is not int or item.get("dir") not in ("in", "out"):
            raise PlanError("requires.wiring entries need pin (int) and dir (in|out)")
    steps = _steps(data.get("steps", []), "steps")
    if not steps:
        raise PlanError("plan has no steps")
    plan = {"schema": SCHEMA, "name": name.strip(), "slug": data.get("slug") or slugify(name),
            "source": str(data.get("source", "")),
            "requires": {"wiring": wiring, "uart": requires.get("uart", []),
                         **({"scenario": requires["scenario"]} if requires.get("scenario") else {})},
            "steps": steps, "cleanup": _steps(data.get("cleanup", []), "cleanup")}
    if data.get("needs"):
        plan["needs"] = list(data["needs"])
    return plan


def plans_dir(home: Path | str | None = None) -> Path:
    return Path(home or home_dir()) / "plans"


def dump_plan(plan: dict) -> str:
    return json.dumps(plan, indent=1, sort_keys=True) + "\n"


def save_plan(plan: dict, home: Path | str | None = None) -> Path:
    plan = validate_plan(plan)
    folder = plans_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{plan['slug']}.json"
    path.write_text(dump_plan(plan), encoding="utf-8")
    return path


def load_plan(slug_or_path: str | Path, home: Path | str | None = None) -> dict:
    path = Path(slug_or_path)
    if path.suffix != ".json":
        for folder in (plans_dir(home), EXAMPLES):
            candidate = folder / f"{slug_or_path}.json"
            if candidate.exists():
                path = candidate
                break
        else:
            known = ", ".join(p["slug"] for p in list_plans(home))
            raise FileNotFoundError(f"unknown plan {slug_or_path!r}; known: {known}")
    return validate_plan(json.loads(path.read_text(encoding="utf-8")))


def list_plans(home: Path | str | None = None) -> list[dict]:
    out, seen = [], set()
    for folder, origin in ((plans_dir(home), "user"), (EXAMPLES, "example")):
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            try:
                plan = validate_plan(json.loads(path.read_text(encoding="utf-8")))
            except (PlanError, ValueError, OSError):
                continue
            if plan["slug"] in seen:
                continue
            seen.add(plan["slug"])
            out.append({"slug": plan["slug"], "name": plan["name"], "source": plan["source"], "origin": origin,
                        "steps": len(plan["steps"]), "assertions": sum(1 for s in plan["steps"] if s.get("assert")),
                        "requires": plan["requires"], "path": str(path)})
    return out
