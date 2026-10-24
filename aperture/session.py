"""Sessions: one connection plus an ordered, monotonic log of everything that happened.

Events are appended to a JSONL journal as they occur and compacted to
``session.json`` on ``end()``/``save()``. Replay and export read ``session.json``
and never need a transport.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .protocol import ProtocolError, _jsonable
from .requests import Request
from .transport.base import Response, Transport, TransportError

SCHEMA = 1
BRIDGE_EVENT_ACTOR = {"pin.change": "rig", "uart.rx": "rig", "capture.done": "bridge", "control.changed": "bridge",
                      "control.lost": "bridge", "stream.overflow": "bridge", "rig.warning": "rig"}


def home_dir() -> Path:
    return Path(os.environ.get("APERTURE_HOME", Path.home() / ".aperture"))


def _text_preview(data: bytes) -> str | None:
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return None
    return text if text.isprintable() or text.strip("\r\n\t").isprintable() else None


class Session:
    def __init__(self, actor: str = "tester", home: Path | str | None = None, persist: bool = True,
                 session_id: str | None = None):
        now = datetime.now(timezone.utc)
        self.id = session_id or f"s_{now:%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"
        self.created_at = now.isoformat(timespec="milliseconds")
        self.actor = actor
        self.home = Path(home) if home else home_dir()
        self.persist = persist
        self.transport: Transport | None = None
        self.manifest: dict | None = None
        self.events: list[dict] = []
        self.requests: dict[str, Request] = {}
        self.wiring: dict[int, dict] = {}
        self.pending_wiring: dict[int, dict] = {}
        self.assertions: list[dict] = []
        self.ended: dict | None = None
        self.verdict: bool | None = None
        self._t0 = time.monotonic_ns()
        self._seq = 0
        self._req_n = 0
        self._pending: Request | None = None
        self._keepalive_gen = 0  # bumped to retire the running keepalive thread
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._listeners: list[Callable[[dict], None]] = []
        self._journal = None
        if self.persist:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._journal = open(self.dir / "journal.jsonl", "a", encoding="utf-8")  # noqa: SIM115

    # --- plumbing -------------------------------------------------------------------
    @property
    def dir(self) -> Path:
        return self.home / "sessions" / self.id

    def now_us(self) -> int:
        return (time.monotonic_ns() - self._t0) // 1000

    def on_event(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def record(self, kind: str, actor: str | None = None, **fields) -> dict:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "t_host_us": self.now_us(), "kind": kind, "actor": actor or self.actor,
                     **_jsonable(fields)}
            self.events.append(event)
            if self._journal:
                self._journal.write(json.dumps(event, separators=(",", ":")) + "\n")
                self._journal.flush()
            self._cond.notify_all()
        for callback in list(self._listeners):
            callback(event)
        return event

    # --- connection ------------------------------------------------------------------
    def connect(self, transport: Transport, take_control: bool = True) -> dict:
        with self._lock:
            if self.transport is not None:
                raise TransportError("session already has a connection; start a new session")
            self.transport = transport
            transport.on_ack(self._on_ack)
            transport.on_event(self._on_bridge_event)
            self._pending = Request("r0", "hello", {"client": transport.client}, "system")
            self.requests["r0"] = self._pending
            self._pending.advance("requested", t_host_us=self.now_us())
            self.record("request.stage", "system", req="r0", stage="requested", op="hello",
                        args=self._pending.args)
            try:
                manifest = transport.connect()
            except Exception as exc:
                self._pending.advance("executed", t_host_us=self.now_us(), ok=False, err="io", msg=str(exc),
                                      ack="implicit")
                self.record("request.stage", "system", req="r0", stage="executed", ok=False, err="io", msg=str(exc))
                self._pending = None
                self.transport = None
                raise
            self._pending.advance("executed", t_host_us=self.now_us(), ok=True, result={"manifest": manifest},
                                  ack=None if "acknowledged" in self._pending.stages else "implicit")
            self.record("request.stage", "system", req="r0", stage="executed", ok=True)
            self._pending.advance("observed", t_host_us=self.now_us(), observe="manifest")
            self.record("request.stage", "system", req="r0", stage="observed", observe="manifest")
            self._pending = None
            self.manifest = manifest
            self.record("session.start", "system", session=self.id, created_at=self.created_at,
                        transport=transport.describe(), manifest=manifest,
                        clock={"offset_us": transport.clock_offset_us})
            if take_control:
                self.request("control.take", actor="system")
            return manifest

    def disconnect(self, reason: str = "user") -> None:
        with self._lock:
            if self.transport is None:
                return
            if self.transport.connected and self.transport.control_token:
                self.request("release_all", actor="system")
                self.request("control.release", actor="system", token=self.transport.control_token)
            self.transport.disconnect()
            self.record("session.end", "system", reason=reason)
            self.end(reason)

    def _start_keepalive(self, watchdog_s: float | None) -> None:
        self._keepalive_gen += 1
        interval = max(0.25, (watchdog_s or 3.0) / 3)
        threading.Thread(target=self._keepalive, args=(self._keepalive_gen, interval),
                         name=f"keepalive-{self.id}", daemon=True).start()

    def _keepalive(self, gen: int, interval: float) -> None:
        """Ping while we hold control, so the bridge watchdog only fires once this host is gone. Each ping runs
        under the session lock like a request (same lock order, never overlaps one, so it is never logged)."""
        while True:
            time.sleep(interval)
            with self._lock:
                transport = self.transport
                if gen != self._keepalive_gen or not (transport and transport.connected and transport.control_token):
                    return
                # a failed ping surfaces on the next real request; a control.lost event clears the token
                with contextlib.suppress(TransportError, ProtocolError, OSError):
                    transport.request("ping")

    def _on_ack(self, request_id: int, t_bridge_us: int) -> None:
        with self._lock:
            request = self._pending
            if request is None or "acknowledged" in request.stages:
                return
            request.advance("acknowledged", t_host_us=self.now_us(), t_bridge_us=t_bridge_us)
        self.record("request.stage", "bridge", req=request.id, stage="acknowledged", t_bridge_us=t_bridge_us)

    def _on_bridge_event(self, message: dict) -> None:
        name = message.get("ev")
        payload = {k: v for k, v in message.items() if k not in ("v", "ev", "t")}
        if name == "uart.rx" and isinstance(payload.get("data"), str):
            import base64
            raw = base64.b64decode(payload["data"])
            payload["text"] = _text_preview(raw)
            payload["dir"] = "rx"
            name = "bus.bytes"
            payload["bus"] = "uart"
        if name == "pin.change":
            payload["source"] = "stream"
        self.record(name, BRIDGE_EVENT_ACTOR.get(message.get("ev"), "bridge"), t_bridge_us=message.get("t"), **payload)

    # --- requests ---------------------------------------------------------------------
    def request(self, op: str, actor: str | None = None, observe: Callable[[Response], dict] | None = None,
                **args) -> Request:
        """Run one protocol op through the full lifecycle. Returns the Request (check ``.ok``)."""
        if self.transport is None:
            raise TransportError("session is not connected")
        with self._lock:
            self._req_n += 1
            request = Request(f"r{self._req_n}", op, _jsonable(args), actor or self.actor)
            self.requests[request.id] = request
            request.advance("requested", t_host_us=self.now_us())
            self.record("request.stage", request.actor, req=request.id, stage="requested", op=op, args=request.args)
            self._pending = request
            try:
                response = self.transport.request(op, **args)
            except (TransportError, OSError, ProtocolError) as exc:
                response = Response(-1, False, 0, {}, "timeout" if "timeout" in str(exc).lower() else "io", str(exc))
            finally:
                self._pending = None
            if op == "control.take" and response.ok:
                self._start_keepalive(response.result.get("watchdog_s"))
            if "acknowledged" not in request.stages:
                request.advance("executed", t_host_us=self.now_us(), ack="implicit", ok=response.ok,
                                err=response.err, result=response.result, t_bridge_us=response.t_bridge_us)
            else:
                request.advance("executed", t_host_us=self.now_us(), ok=response.ok, err=response.err,
                                result=response.result, t_bridge_us=response.t_bridge_us)
            self.record("request.stage", "bridge", req=request.id, stage="executed", ok=response.ok,
                        err=response.err, msg=response.msg, result=response.result, t_bridge_us=response.t_bridge_us)
            if op == "uart.write":
                self.record("bus.bytes", request.actor, bus="uart", dir="tx", port=args.get("port"),
                            data=args.get("data"), text=_text_preview(args.get("data", b"")))
            observation: dict
            if not response.ok:
                observation = {"observe": "n/a", "result": "failed"}
            elif observe is None:
                observation = {"observe": "n/a"}
            else:
                try:
                    observation = observe(response) or {"observe": "n/a"}
                except (TransportError, ProtocolError, OSError) as exc:
                    observation = {"observe": "error", "msg": str(exc)}
            request.advance("observed", t_host_us=self.now_us(), **observation)
            self.record("request.stage", "bridge", req=request.id, stage="observed", **observation)
            return request

    def wait_for_event(self, predicate: Callable[[dict], bool], timeout_ms: int,
                       since_seq: int | None = None) -> dict | None:
        """Block until an event matching ``predicate`` is recorded (or timeout). On a Virtual Rig with a
        manual clock the rig clock is advanced instead of sleeping, so tests stay deterministic."""
        start_seq = self._seq if since_seq is None else since_seq
        rig = getattr(self.transport, "rig", None)
        manual = rig is not None and not rig.clock.realtime
        deadline = time.monotonic() + timeout_ms / 1000
        elapsed_us = 0
        with self._cond:
            while True:
                for event in self.events[start_seq:]:
                    if predicate(event):
                        return event
                if manual:
                    if elapsed_us >= timeout_ms * 1000:
                        return None
                    step = min(1000, timeout_ms * 1000 - elapsed_us)
                    self._lock.release()
                    try:
                        rig.advance(step)
                    finally:
                        self._lock.acquire()
                    elapsed_us += step
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(min(remaining, 0.05))
                    if rig is not None:
                        rig.pump()

    # --- notes, wiring, assertions ------------------------------------------------------
    def note(self, text: str, actor: str | None = None) -> dict:
        return self.record("note", actor, text=text)

    def request_wiring(self, pin: int, dut_pin: str, component: str, reason: str, direction: str,
                       actor: str | None = None) -> dict:
        entry = {"pin": pin, "dut_pin": dut_pin, "component": component, "reason": reason, "dir": direction}
        with self._lock:
            self.pending_wiring[pin] = entry
        self.record("wiring.request", actor or "agent", **entry)
        return entry

    def confirm_wiring(self, pin: int, confirmed: bool, by: str | None = None) -> dict:
        with self._lock:
            entry = self.pending_wiring.pop(pin, None)
            if entry is None:
                raise ValueError(f"no pending wiring request for GPIO{pin}")
            if confirmed:
                self.wiring[pin] = {**entry, "by": by or self.actor}
        self.record("wiring.confirmed" if confirmed else "wiring.rejected", by or self.actor, **entry)
        return entry

    def assertion(self, name: str, passed: bool, expected, actual, actor: str | None = None, **detail) -> dict:
        event = self.record("assertion", actor, name=name, **{"pass": bool(passed)}, expected=expected,
                            actual=actual, **detail)
        self.assertions.append(event)
        self.verdict = all(a["pass"] for a in self.assertions)
        return event

    # --- persistence --------------------------------------------------------------------
    def to_dict(self) -> dict:
        transport = self.transport.describe() if self.transport else None
        return {"schema": SCHEMA, "id": self.id, "created_at": self.created_at, "actor": self.actor,
                "transport": transport, "manifest": self.manifest,
                "clock": {"offset_us": self.transport.clock_offset_us if self.transport else None},
                "wiring": {str(k): v for k, v in self.wiring.items()},
                "verdict": self.verdict, "ended": self.ended, "events": list(self.events)}

    def save(self, path: Path | str | None = None) -> Path:
        path = Path(path) if path else self.dir / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def end(self, reason: str = "user") -> None:
        with self._lock:
            if self.ended:
                return
            self.ended = {"reason": reason, "t_host_us": self.now_us()}
            if self._journal:
                self._journal.close()
                self._journal = None
            if self.persist:
                self.save()

    def close(self) -> None:
        if self.transport and self.transport.connected:
            self.disconnect("close")
        else:
            self.end("close")


# --- loading / listing -------------------------------------------------------------------
def load_session(path_or_id: str | Path, home: Path | str | None = None) -> dict:
    path = Path(path_or_id)
    if path.suffix != ".json":
        path = Path(home or home_dir()) / "sessions" / str(path_or_id) / "session.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"unsupported session schema {data.get('schema')!r}")
    return data


def dump_session(data: dict) -> str:
    return json.dumps(data, indent=1, sort_keys=True) + "\n"


def summarize(data: dict) -> dict:
    return {"id": data["id"], "created_at": data["created_at"],
            "transport": (data.get("transport") or {}).get("kind"),
            "bridge": (data.get("manifest") or {}).get("bridge"),
            "scenario": (data.get("transport") or {}).get("scenario"),
            "event_count": len(data.get("events", [])), "verdict": data.get("verdict"),
            "ended": data.get("ended"), "name": data.get("name")}


def list_sessions(home: Path | str | None = None) -> list[dict]:
    root = Path(home or home_dir()) / "sessions"
    out = []
    for path in root.glob("*/session.json"):
        try:
            out.append((path.stat().st_mtime_ns, summarize(json.loads(path.read_text(encoding="utf-8")))))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda item: (item[1]["created_at"], item[0]), reverse=True)
    return [item[1] for item in out]
