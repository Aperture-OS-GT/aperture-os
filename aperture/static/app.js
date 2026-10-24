/* Aperture OS tester console. One reducer renders both the live feed and replayed sessions. */
(() => {
  const $ = (s) => document.querySelector(s);
  const api = async (path, body) => {
    const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  };
  const toast = (msg, ms = 2600) => { const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), ms); };
  const fmtT = (us, t0) => us == null ? "" : ((us - (t0 ?? us)) / 1e6).toFixed(3) + " s";

  const S = { state: null, events: [], t0: null, cursor: 0, selected: 17, filters: new Set(["agent", "tester", "bridge", "pin", "bus", "assert"]), search: "",
              replay: null, rp: { idx: 0, timer: null, speed: 1 }, pinHistory: {}, reqs: {} };

  // ---------- reducer: session events -> view model ----------
  const classify = (e) => {
    if (e.kind === "request.stage") return e.actor === "agent" ? "agent" : e.actor === "tester" ? "tester" : "bridge";
    if (e.kind === "pin.change") return "pin";
    if (e.kind === "bus.bytes") return "bus";
    if (e.kind === "assertion" || e.kind.startsWith("plan.")) return "assert";
    if (e.kind === "note") return e.actor === "agent" ? "agent" : e.actor === "tester" ? "tester" : "bridge";
    if (e.kind.startsWith("wiring.") || e.kind.startsWith("wait.")) return "agent";
    return "bridge";
  };
  const reduce = (e) => {
    if (S.t0 == null && e.t_host_us != null) S.t0 = e.t_host_us;
    if (e.kind === "request.stage") {
      const r = S.reqs[e.req] || (S.reqs[e.req] = { id: e.req, op: e.op, args: e.args, actor: e.actor, stages: {}, el: null });
      r.stages[e.stage] = e; if (e.op) r.op = e.op; if (e.args) r.args = e.args;
      if (e.ok === false) r.err = e.err; if (e.mismatch) r.mismatch = true;
      return r;
    }
    if (e.kind === "pin.change") (S.pinHistory[e.pin] ||= []).push([e.t_bridge_us ?? e.t_host_us, e.level]);
    return null;
  };

  // ---------- log rendering ----------
  const log = $("#log");
  const stagePill = (r, name, label) => {
    const st = r.stages[name]; if (!st) return `<span class="st">${label}</span>`;
    let cls = "done"; if (name === "observed") cls = r.mismatch ? "mis" : "obs"; if (name === "executed" && r.err) cls = "err";
    const dt = r.stages.requested && st.t_host_us != null ? ` +${((st.t_host_us - r.stages.requested.t_host_us) / 1000).toFixed(1)}ms` : "";
    return `<span class="st ${cls}" title="${name}${dt}">${label}${name === "executed" && r.err ? " " + r.err : ""}</span>`;
  };
  const renderRequest = (r) => {
    const stages = `<span class="stages">${stagePill(r, "requested", "req")}${stagePill(r, "acknowledged", "ack")}${stagePill(r, "executed", "exec")}${stagePill(r, "observed", "obs")}</span>`;
    const args = r.args ? Object.entries(r.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ") : "";
    const obs = r.stages.observed; let detail = "";
    if (obs && obs.level != null) detail = ` → level ${obs.level}${r.mismatch ? " (MISMATCH)" : ""}`;
    if (r.stages.executed && r.stages.executed.msg) detail = ` — ${r.stages.executed.msg}`;
    return `<span class="who">${r.actor}</span><code>${r.op}</code>${r.count ? ` <span class="st">×${r.count}</span>` : ""} <span class="mono muted">${args}</span>${stages}<span class="mono">${detail}</span>`;
  };
  const renderEvent = (e) => {
    switch (e.kind) {
      case "pin.change": return `<span class="who">bridge</span>GPIO${e.pin} → <b>${e.level ? "HIGH" : "LOW"}</b> <span class="muted">(${e.source || "stream"})</span>`;
      case "bus.bytes": return `<span class="who">${e.dir === "tx" ? "host → DUT" : "DUT → host"}</span><code>${e.bus}${e.port != null ? e.port : ""}</code> <pre>${escapeHtml(e.text ?? e.hex ?? "")}</pre>`;
      case "assertion": return `<span class="who">${e.actor}</span><b class="${e.pass ? "ok" : "bad"}">${e.pass ? "PASS" : "FAIL"}</b> ${escapeHtml(e.name)} <span class="mono muted">expected ${JSON.stringify(e.expected)} got ${JSON.stringify(e.actual)}</span>`;
      case "note": return `<span class="who">${e.actor}</span>${escapeHtml(e.text)}`;
      case "wiring.request": return `<span class="who">agent</span>Wants GPIO${e.pin} (${e.dir}) → ${escapeHtml(e.dut_pin)} / ${escapeHtml(e.component)}: ${escapeHtml(e.reason)} <button class="link" data-wire="${e.pin}" data-ok="1">confirm</button> <button class="link" data-wire="${e.pin}" data-ok="0">reject</button>`;
      case "wiring.confirmed": return `<span class="who">tester</span>Confirmed wiring GPIO${e.pin} → ${escapeHtml(e.dut_pin || "")}`;
      case "wiring.rejected": return `<span class="who">tester</span>Rejected wiring GPIO${e.pin}`;
      case "wait.timeout": return `<span class="who">agent</span>Timed out waiting for GPIO${e.pin} ${e.edge} (${e.timeout_ms} ms)`;
      case "wait.hit": return `<span class="who">agent</span>GPIO${e.pin} ${e.edge} after ${(e.elapsed_us / 1000).toFixed(2)} ms`;
      case "capture.done": return `<span class="who">bridge</span>Capture #${e.capture_id}: ${(e.edges || []).length} edges on GPIO ${(e.pins || []).join(",")}`;
      case "plan.start": return `<span class="who">plan</span>▶ ${escapeHtml(e.name)} <span class="muted">(${e.steps} steps)</span>`;
      case "plan.step": return `<span class="who">plan</span>${e.pass ? "✓" : "✗"} ${e.step} <code>${e.tool}</code>${e.error ? ` <span class="bad">${escapeHtml(e.error.msg)}</span>` : ""}`;
      case "plan.end": return `<span class="who">plan</span><b class="${e.pass ? "ok" : "bad"}">${e.pass ? "PASS" : "FAIL"}</b> ${e.plan}${e.failed_steps?.length ? " failed: " + e.failed_steps.join(", ") : ""}`;
      case "plan.blocked": return `<span class="who">plan</span>Blocked: confirm the wiring requests below for ${e.missing.map((w) => "GPIO" + w.pin).join(", ")}, then run the plan again`;
      case "session.start": return `<span class="who">system</span>Session started · ${e.transport?.kind || ""} ${e.transport?.scenario || e.transport?.target || ""}`;
      case "session.end": return `<span class="who">system</span>Session ended (${e.reason})`;
      case "control.taken": case "control.released": case "control.lost": return `<span class="who">bridge</span>${e.kind.replace("control.", "control ")}${e.by ? " by " + e.by : ""}`;
      case "bridge.event": return `<span class="who">bridge</span>${e.ev} <span class="mono muted">${escapeHtml(JSON.stringify(e.payload || {}))}</span>`;
      default: return `<span class="who">${e.actor}</span>${e.kind} <span class="mono muted">${escapeHtml(JSON.stringify(stripMeta(e)))}</span>`;
    }
  };
  const stripMeta = (e) => { const o = { ...e }; for (const k of ["seq", "t_host_us", "kind", "actor", "cursor", "session"]) delete o[k]; return o; };
  const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const visible = (e, cls) => S.filters.has(cls) && (!S.search || JSON.stringify(e).toLowerCase().includes(S.search));

  // ---------- DUT model: LCD text + status LED, followed from UART traffic ----------
  const DUT = { lcd: ["", ""], led: { mode: "blink", hz: 2 }, tx: "", rx: "" };
  const renderDut = () => {
    $("#lcd-1").textContent = DUT.lcd[0].slice(0, 16); $("#lcd-2").textContent = DUT.lcd[1].slice(0, 16);
    const live = !!S.state?.connection.connected, led = $("#led");
    led.className = `led ${live ? DUT.led.mode : "off"}`; led.style.setProperty("--period", `${1 / (DUT.led.hz || 2)}s`);
    $("#led-mode").textContent = !live ? "off (no rig)" : DUT.led.mode === "blink" ? `blinking ${DUT.led.hz} Hz` : DUT.led.mode;
  };
  const dutLine = (dir, line) => {
    const t = line.trim(); let m;
    if (dir === "tx" && (m = t.match(/^LCD (.*?)\|(.*)$/))) DUT.lcd = [m[1], m[2]];
    else if (dir === "tx" && (m = t.match(/^LED BLINK ([\d.]+)/i))) DUT.led = { mode: "blink", hz: +m[1] || 2 };
    else if (dir === "tx" && /^LED ON\b/i.test(t)) DUT.led = { ...DUT.led, mode: "on" };
    else if (dir === "tx" && /^LED OFF\b/i.test(t)) DUT.led = { ...DUT.led, mode: "off" };
    // boot defaults only until we've commanded it (the virtual DUT re-announces BOOT OK whenever UART is reopened)
    else if (dir === "rx" && /BOOT OK/.test(t)) { if (DUT.commanded) return; DUT.lcd = ["BOOT OK", ""]; DUT.led = { mode: "blink", hz: 2 }; }
    else return;
    if (dir === "tx") DUT.commanded = true;
    renderDut();
  };
  const dutBytes = (e) => {
    const k = e.dir === "tx" ? "tx" : "rx"; const lines = (DUT[k] + (e.text ?? "")).split(/\r?\n/); DUT[k] = lines.pop();
    lines.forEach((l) => dutLine(k, l)); if (k === "rx" && /BOOT OK/.test(DUT[k])) { dutLine("rx", DUT[k]); DUT[k] = ""; }
  };

  const appendEvent = (e) => {
    if (e.kind === "bus.bytes") dutBytes(e);
    const r = reduce(e);
    const cls = classify(e);
    if (r) {
      // collapse back-to-back identical polls (uart.read / pin.read loops) into one row with a counter
      if (!r.el && e.stage === "requested" && S.lastReq && S.lastReq.id !== r.id && S.lastReq.op === e.op && ["uart.read", "pin.read"].includes(e.op)
          && JSON.stringify(S.lastReq.args) === JSON.stringify(e.args) && S.lastReq.stages.observed) { r.el = S.lastReq.el; r.count = (S.lastReq.count || 1) + 1; }
      if (e.stage === "requested") { S.lastReq = r; if (!["uart.read", "pin.read"].includes(e.op)) S.lastBus = null; }
      if (!r.el) { r.el = document.createElement("div"); r.el.className = `ev ${cls}`; r.el.innerHTML = `<div class="t">${fmtT(e.t_host_us, S.t0)}</div><div class="body"></div>`; log.appendChild(r.el); }
      r.el.querySelector(".body").innerHTML = renderRequest(r);
      r.el.classList.toggle("bad", !!r.err); r.el.hidden = !visible(e, cls);
    } else {
      if (e.kind !== "pin.change" && e.kind !== "bus.bytes") { S.lastReq = null; S.lastBus = null; }
      const last = S.lastBus;
      if (e.kind === "bus.bytes" && last && last._ev.bus === e.bus && last._ev.dir === e.dir && last._ev.port === e.port) {
        const pre = last.querySelector("pre"); pre.textContent += e.text ?? e.hex ?? ""; last._ev = { ...last._ev, text: pre.textContent }; return;
      }
      const el = document.createElement("div"); el.className = `ev ${cls}${e.kind === "assertion" && !e.pass ? " bad" : ""}`;
      el.dataset.kind = e.kind; el.innerHTML = `<div class="t">${fmtT(e.t_host_us, S.t0)}</div><div class="body">${renderEvent(e)}</div>`;
      el.hidden = !visible(e, cls); el._ev = e; log.appendChild(el);
      if (e.kind === "bus.bytes") S.lastBus = el;
    }
    if (e.kind === "pin.change" && e.pin === S.selected) renderInspector();
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    if (nearBottom) log.scrollTop = log.scrollHeight;
  };
  const resetLog = () => { log.innerHTML = ""; S.events = []; S.t0 = null; S.reqs = {}; S.pinHistory = {}; S.lastReq = null; S.lastBus = null; };
  const refilter = () => { for (const el of log.children) { const e = el._ev || Object.values(S.reqs).find((r) => r.el === el)?.stages.requested; if (e) el.hidden = !visible(e, [...el.classList].find((c) => S.filters.has(c) || ["agent", "tester", "bridge", "pin", "bus", "assert"].includes(c))); } };

  // ---------- state / rig panel ----------
  const applyState = (st) => {
    S.state = st; $("#version").textContent = "v" + st.version;
    const c = st.connection, m = st.manifest || {};
    const status = $("#conn-status"); status.className = "status" + (c.connected ? (c.transport?.kind === "virtual" ? " on virtual" : " on") : "");
    status.querySelector("span").textContent = c.connected ? (c.transport?.kind === "virtual" ? `Virtual Rig · ${c.transport.scenario}` : `Connected · ${c.transport?.target || c.transport?.kind}`) : "Disconnected";
    $("#rig-bridge").textContent = m.bridge || "—"; $("#rig-fw").textContent = m.fw || "—"; $("#rig-board").textContent = m.board || "—";
    $("#rig-power").textContent = m.power ? (Object.entries(m.power).filter(([, v]) => v != null).map(([k, v]) => `${k.replace(/_/g, " ")} ${v === true ? "ok" : v === false ? "FAIL" : v}`).join(" · ") || "n/a") : "—";
    $("#rig-control").textContent = c.connected ? (c.controller ? "you (aperture-host)" : "none") : "—";
    $("#btn-connect").disabled = c.connected; $("#btn-disconnect").disabled = !c.connected; $("#btn-control").disabled = !c.connected;
    $("#btn-control").textContent = c.controller ? "Release control" : "Take control"; $("#btn-release").disabled = !c.controller;
    $("#session-id").textContent = st.session.id;
    const v = $("#verdict"); v.className = "verdict" + (st.session.verdict === true ? " pass" : st.session.verdict === false ? " fail" : ""); v.textContent = st.session.verdict === true ? "PASS" : st.session.verdict === false ? "FAIL" : "";
    $("#export-json").href = `/api/sessions/${st.session.id}/export.json`; $("#export-json").download = `${st.session.id}.json`;
    $("#export-md").href = `/api/sessions/${st.session.id}/export.md`; $("#export-md").download = `${st.session.id}.md`;
    const beh = $("#behaviors"); beh.innerHTML = "";
    for (const b of st.behaviors || []) {
      const chip = document.createElement("span"); chip.className = "chip"; chip.textContent = `${b.type} ${b.name || ""}`;
      if (b.type === "Button") for (const a of ["press", "release"]) { const bt = document.createElement("button"); bt.textContent = a; bt.onclick = () => tool("dut_action", { behavior: b.name, action: a }); chip.appendChild(bt); }
      if (b.type === "ScriptedDut") { const bt = document.createElement("button"); bt.textContent = "reset"; bt.onclick = () => tool("dut_action", { behavior: "dut", action: "reset" }); chip.appendChild(bt); }
      beh.appendChild(chip);
    }
    const scen = $("#scenario"); if (!scen.options.length) for (const s of st.scenarios) { const o = document.createElement("option"); o.value = s.name; o.textContent = s.title || s.name; o.title = s.description; scen.appendChild(o); }
    $("#replay-pill").hidden = !st.replay; $("#replay-bar").hidden = !st.replay; $("#composer").hidden = !!st.replay;
    if (!c.connected) { S.uartPort = null; DUT.commanded = false; }
    renderHeader(); renderInspector(); renderDut();
  };
  const refresh = async () => applyState(await api("/api/state"));

  // ---------- header SVG ----------
  const PITCH = 2.54 * 9.5; // to scale: 2.54 mm pitch rendered at 9.5 px/mm
  // bench wiring Pi -> ESP32 DUT (out = Pi drives it); keyed by BCM, power/ground by physical pin
  const WIRES = { 17: ["D4", 1], 18: ["D5", 1], 27: ["D18", 1], 22: ["D19", 0], 23: ["D21", 0], 14: ["RX0", 1], 15: ["TX0", 0], 25: ["EN", 1], 24: ["BOOT", 1] };
  const PHYS_WIRES = { 2: ["VIN", 1], 6: ["GND", 1] };
  const renderHeader = () => {
    const svg = $("#header-svg"); const flat = S.state?.header || []; if (!flat.length) return;
    const pins = S.state?.pins || {}; const w = 190, pad = 14; svg.setAttribute("viewBox", `0 0 ${w} ${pad * 2 + PITCH * 20}`);
    let out = `<rect x="${w / 2 - PITCH - 8}" y="${pad - 10}" width="${PITCH * 2 + 16}" height="${PITCH * 20 + 4}" rx="4" fill="#efeee8" stroke="#cfcdc5" pointer-events="none"/>`;
    for (const p of flat) {
      const col = (p.phys - 1) % 2, row = Math.floor((p.phys - 1) / 2);
      const cx = w / 2 - PITCH / 2 + col * PITCH, cy = pad + row * PITCH + PITCH / 2;
      let cls = "pin-g"; const gpio = p.kind === "gpio" ? p.bcm : null; const st = gpio != null ? pins[String(gpio)] : null;
      if (p.kind === "gnd") cls += " gnd"; else if (p.kind === "5v") cls += " power"; else if (p.kind === "3v3") cls += " v33";
      else if (st) { cls += st.mode === "output" ? " out" : st.mode === "pwm" ? " pwm" : String(st.mode).startsWith("input") ? " in" : ""; if (st.level === 1) cls += " hi"; }
      if (gpio != null && gpio === S.selected) cls += " sel";
      const label = gpio != null ? `G${gpio}` : p.kind === "gnd" ? "GND" : p.kind === "reserved" ? "ID" : p.kind.toUpperCase();
      const tx = col ? cx + PITCH / 2 + 6 : cx - PITCH / 2 - 6, anchor = col ? "start" : "end";
      const wire = gpio != null ? WIRES[gpio] : PHYS_WIRES[p.phys]; let text = `${label}${p.alt ? "·" + p.alt : ""}`;
      if (wire) text = col ? `${text}<tspan class="wire"> ${wire[1] ? "▸" : "◂"} ${wire[0]}</tspan>` : `<tspan class="wire">${wire[0]} ${wire[1] ? "◂" : "▸"} </tspan>${text}`;
      out += `<g class="${cls}" data-gpio="${gpio ?? ""}" data-phys="${p.phys}"><circle cx="${cx}" cy="${cy}" r="${PITCH * 0.36}"/><text x="${cx}" y="${cy + 2.5}" text-anchor="middle" class="in">${p.phys}</text><text x="${tx}" y="${cy + 2.5}" text-anchor="${anchor}">${text}</text></g>`;
    }
    svg.innerHTML = out;
    svg.querySelectorAll(".pin-g").forEach((g) => { if (g.dataset.gpio !== "") g.onclick = () => { S.selected = +g.dataset.gpio; renderHeader(); renderInspector(); }; });
  };

  // ---------- inspector ----------
  const renderInspector = () => {
    const pin = S.selected; const st = S.state?.pins?.[String(pin)]; const header = S.state?.header || [];
    const meta = header.find((p) => p.kind === "gpio" && p.bcm === pin);
    $("#insp-title").textContent = `GPIO${pin}`; $("#insp-phys").textContent = meta ? `phys ${meta.phys}${meta.alt ? " · " + meta.alt : ""} · ${(meta.caps || []).join(" ")}` : "";
    const body = $("#insp-body"); const wiring = S.state?.session.wiring?.[String(pin)]; const pending = S.state?.session.pending_wiring?.[String(pin)];
    if (!S.state?.connection.connected) { body.textContent = "Connect to a rig to inspect pins."; $("#insp-actions").hidden = true; $("#insp-wiring").hidden = true; return; }
    body.innerHTML = st ? `<dl class="kv"><dt>Mode</dt><dd>${st.mode}</dd><dt>Level</dt><dd>${st.level == null ? "floating" : st.level ? "HIGH" : "LOW"}</dd>${st.pwm ? `<dt>PWM</dt><dd>${st.pwm.freq_hz} Hz · ${Math.round(st.pwm.duty * 100)}%</dd>` : ""}<dt>Caps</dt><dd>${(st.caps || []).join(", ")}</dd><dt>Wiring</dt><dd>${wiring ? `${wiring.dut_pin} / ${wiring.component} ✓` : pending ? "pending" : "—"}</dd></dl>` : "not a GPIO";
    $("#insp-actions").hidden = !st; $("#insp-mode").value = st?.mode || "input";
    $("#insp-wiring").hidden = !pending; if (pending) $("#insp-wiring-text").textContent = `${pending.dut_pin} / ${pending.component} (${pending.direction}) — ${pending.reason}`;
    const hist = (S.pinHistory[pin] || []).slice(-60); const spark = $("#insp-spark");
    if (hist.length < 1) { spark.innerHTML = ""; return; }
    const t0 = hist[0][0], t1 = hist[hist.length - 1][0] || t0 + 1, W = 300, H = 40; const x = (t) => 4 + ((t - t0) / Math.max(1, t1 - t0)) * (W - 8);
    let d = `M${x(hist[0][0])},${hist[0][1] ? 6 : H - 6}`; for (let i = 1; i < hist.length; i++) { d += ` H${x(hist[i][0])} V${hist[i][1] ? 6 : H - 6}`; } d += ` H${W - 4}`;
    spark.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="#3975d7" stroke-width="2"/></svg>`;
  };

  // ---------- actions ----------
  const tool = async (name, args) => { try { const r = await api("/api/tool", { tool: name, args, actor: "tester" }); if (!r.ok) toast(`${name}: ${r.error.msg}`); await refresh(); return r; } catch (e) { toast(e.message); } };
  $("#btn-connect").onclick = async () => {
    const body = { transport: $("#transport").value, scenario: $("#scenario").value, target: $("#target").value || null };
    try { applyState(await api("/api/connect", body)); }
    catch (e) {
      // a session that was connected once can't reconnect: start a fresh session and retry
      if (!/new session/.test(e.message)) { toast(e.message); return; }
      try { resetLog(); await api("/api/session/new", {}); applyState(await api("/api/connect", body)); } catch (e2) { toast(e2.message); }
    }
    loadSessions();
  };
  $("#btn-disconnect").onclick = async () => applyState(await api("/api/disconnect", {}));
  $("#btn-control").onclick = () => tool(S.state.connection.controller ? "release_control" : "take_control", {});
  $("#btn-release").onclick = () => tool("release_all", {});
  $("#btn-newsession").onclick = async () => { resetLog(); applyState(await api("/api/session/new", {})); loadSessions(); };
  $("#transport").onchange = () => { const v = $("#transport").value; $("#scenario").hidden = v !== "virtual"; $("#target").hidden = v === "virtual"; };
  $("#insp-apply").onclick = () => { const mode = $("#insp-mode").value; tool("set_mode", mode === "output" ? { pin: S.selected, mode, initial: 0 } : { pin: S.selected, mode }); };
  $("#insp-hi").onclick = () => tool("write", { pin: S.selected, level: 1 }); $("#insp-lo").onclick = () => tool("write", { pin: S.selected, level: 0 });
  $("#insp-pulse").onclick = () => tool("pulse", { pin: S.selected, level: 1, us: 1000 });
  $("#insp-wire-ok").onclick = () => wiring(S.selected, true); $("#insp-wire-no").onclick = () => wiring(S.selected, false);
  const wiring = async (pin, ok) => {
    try { await api(`/api/wiring/${pin}`, { confirmed: ok }); log.querySelectorAll(`[data-wire="${pin}"]`).forEach((b) => b.replaceWith(Object.assign(document.createElement("span"), { className: "muted", textContent: b.dataset.ok === "1" ? (ok ? " ✓ confirmed" : " ✗ rejected") : "" }))); await refresh(); }
    catch (e) { toast(e.message); }
  };
  log.addEventListener("click", (ev) => { const b = ev.target.closest("[data-wire]"); if (b) wiring(+b.dataset.wire, b.dataset.ok === "1"); });
  $("#filters").addEventListener("click", (ev) => { const chip = ev.target.closest("[data-f]"); if (!chip) return; const f = chip.dataset.f, on = !S.filters.has(f); on ? S.filters.add(f) : S.filters.delete(f); chip.setAttribute("aria-pressed", String(on)); refilter(); });
  $("#search").oninput = (ev) => { S.search = ev.target.value.toLowerCase(); refilter(); };
  $("#composer").onsubmit = async (ev) => {
    ev.preventDefault(); const text = $("#prompt").value.trim(); if (!text) return; $("#prompt").value = "";
    try { const r = await api("/api/prompt", { prompt: text }); if (r.plan) showPlan(r.plan); await refresh(); } catch (e) { toast(e.message); }
  };
  const showPlan = (plan) => {
    const node = $("#tpl-plan-run").content.firstElementChild.cloneNode(true);
    node.querySelector(".plan-title").textContent = `${plan.name}${plan.needs ? " — needs: " + plan.needs.join("; ") : ""}`;
    const ol = node.querySelector(".plan-steps"); for (const s of plan.steps) { const li = document.createElement("li"); li.textContent = `${s.tool} ${JSON.stringify(s.args)}${s.assert ? "  [assert]" : ""}`; li.dataset.step = s.id; ol.appendChild(li); }
    node.querySelector(".run").onclick = async () => { try { const r = await api("/api/plans/_inline/run", { plan }); for (const st of r.steps) { const li = ol.querySelector(`[data-step="${st.id}"]`); if (li) li.classList.add(st.pass ? "pass" : "fail"); } toast(r.pass ? "Plan PASS" : "Plan FAIL"); await refresh(); } catch (e) { toast(e.message); } };
    node.querySelector(".save").onclick = async () => { try { const r = await api("/api/plans", plan); toast(`saved ${r.slug}`); loadPlans(); } catch (e) { toast(e.message); } };
    const wrap = document.createElement("div"); wrap.className = "ev agent"; wrap.innerHTML = `<div class="t"></div><div class="body"></div>`; wrap.querySelector(".body").appendChild(node); log.appendChild(wrap); log.scrollTop = log.scrollHeight;
  };

  // ---------- navbar popovers (rig panel, export menu) ----------
  const popovers = [...document.querySelectorAll(".pop-toggle")].map((btn) => ({ btn, pop: document.getElementById(btn.getAttribute("aria-controls")) }));
  const setOpen = (p, open) => { p.pop.hidden = !open; p.btn.setAttribute("aria-expanded", String(open)); };
  for (const p of popovers) {
    p.btn.onclick = () => { const open = p.pop.hidden; popovers.forEach((q) => setOpen(q, q === p && open)); };
    p.btn.parentElement.addEventListener("focusout", (ev) => { if (ev.relatedTarget && !p.btn.parentElement.contains(ev.relatedTarget)) setOpen(p, false); });
  }
  // composedPath() is captured at dispatch, so clicks on controls that re-render (behavior chips) still count as inside
  document.addEventListener("click", (ev) => { const path = ev.composedPath(); for (const p of popovers) if (!path.includes(p.pop) && !path.includes(p.btn)) setOpen(p, false); });
  document.addEventListener("keydown", (ev) => { const p = popovers.find((q) => !q.pop.hidden); if (ev.key === "Escape" && p) { setOpen(p, false); p.btn.focus(); } });
  $("#export-menu").addEventListener("click", (ev) => { if (ev.target.closest("a")) popovers.forEach((p) => setOpen(p, false)); });

  // ---------- sessions & plans lists ----------
  const fmtWhen = (iso) => {
    const d = new Date(iso); if (Number.isNaN(d.getTime())) return iso;
    const day = d.toDateString() === new Date().toDateString() ? "Today" : d.toLocaleDateString([], { month: "short", day: "numeric" });
    return `${day} · ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}`;
  };
  const loadSessions = async () => {
    const { sessions, current } = await api("/api/sessions"); const ul = $("#session-list"); ul.innerHTML = "";
    for (const s of sessions) {
      const li = document.createElement("li"); li.title = s.id; if (s.id === current) li.classList.add("current"); if (S.replay?.id === s.id) li.classList.add("replaying");
      const meta = [[s.transport, s.scenario].filter(Boolean).join(" · "), s.bridge, `${s.event_count} event${s.event_count === 1 ? "" : "s"}`].filter(Boolean).map(escapeHtml).join(" · ");
      const tag = s.verdict === true ? '<span class="v pass">PASS</span>' : s.verdict === false ? '<span class="v fail">FAIL</span>' : s.id === current ? '<span class="v live">LIVE</span>' : "";
      li.innerHTML = `<div><div>${escapeHtml(s.name || fmtWhen(s.created_at))}</div><div class="meta">${meta}</div></div>${tag}`;
      li.onclick = () => (s.id === current ? exitReplay() : startReplay(s.id)); ul.appendChild(li);
    }
  };
  $("#sessions-refresh").onclick = () => loadSessions();

  // ---------- DUT controls: UART commands to the ESP32 (G14/G15) and the demo test ----------
  const dutSend = async (cmd) => {
    if (S.uartPort == null) { const r = await tool("uart_open", { tx: 14, rx: 15, baud: 115200 }); if (!r?.ok) return; S.uartPort = r.result?.port ?? 0; }
    const r = await tool("uart_send", { port: S.uartPort, data: `${cmd}\n` }); if (r?.ok) dutLine("tx", cmd); else S.uartPort = null;
  };
  // the human's click is the wiring confirmation the bridge asks for; only the documented bench wiring is confirmed
  $("#dut-wire").onclick = async () => {
    try {
      for (const [pin, [dut, out]] of Object.entries(WIRES)) {
        if (S.state?.session.wiring?.[pin]) continue;
        if (!S.state?.session.pending_wiring?.[pin]) await api("/api/tool", { tool: "request_wiring", args: { pin: +pin, dut_pin: dut, component: "ESP32 DUT", reason: "bench wiring", dir: out ? "out" : "in" }, actor: "tester" });
        await api(`/api/wiring/${pin}`, { confirmed: true });
      }
      toast("Bench wiring confirmed"); await refresh();
    } catch (e) { toast(e.message); }
  };
  $("#lcd-send").onclick = () => dutSend(`LCD ${$("#lcd-a").value}|${$("#lcd-b").value}`);
  document.querySelectorAll("[data-led]").forEach((b) => { b.onclick = () => dutSend(b.dataset.led); });
  $("#dut-test").onclick = async () => {
    const v = $("#dut-verdict"); v.className = "verdict"; v.textContent = "running…";
    try { const r = await api("/api/plans/esp32-safety/run", {}); v.className = "verdict" + (r.blocked ? "" : r.pass ? " pass" : " fail"); v.textContent = r.blocked ? "confirm wiring" : r.pass ? "PASS" : "FAIL"; toast(r.blocked ? "Blocked: confirm the wiring requests in the log" : r.pass ? "Demo test PASS" : "Demo test FAIL"); await refresh(); }
    catch (e) { v.textContent = ""; toast(e.message); }
  };
  const loadPlans = async () => {
    const { plans } = await api("/api/plans"); const ul = $("#plan-list"); ul.innerHTML = "";
    for (const p of plans) { const li = document.createElement("li"); li.innerHTML = `<div><div>${p.name}</div><div class="meta">${p.steps} steps · ${p.assertions} asserts · ${p.origin}</div></div><button class="link">Run</button>`;
      li.querySelector("button").onclick = async (ev) => { ev.stopPropagation(); try { const r = await api(`/api/plans/${p.slug}/run`, {}); toast(r.blocked ? "Blocked: confirm wiring first" : r.pass ? "Plan PASS" : "Plan FAIL"); await refresh(); } catch (e) { toast(e.message); } };
      li.onclick = async () => showPlan(await api(`/api/plans/${p.slug}`)); ul.appendChild(li); }
  };

  // ---------- replay ----------
  const startReplay = async (id) => {
    try { const data = await api(`/api/sessions/${id}/replay`, {}); S.replay = data; resetLog(); S.rp.idx = 0; $("#rp-pos").max = data.events.length; $("#rp-pos").value = 0; await refresh(); stepReplayTo(data.events.length, true); loadSessions(); } catch (e) { toast(e.message); }
  };
  const exitReplay = async () => { if (S.rp.timer) clearInterval(S.rp.timer); S.rp.timer = null; S.replay = null; resetLog(); await api("/api/replay/stop", {}); const cur = await api(`/api/sessions/${S.state.session.id}`); for (const e of cur.events) appendEvent(e); await refresh(); loadSessions(); };
  const stepReplayTo = (n, instant) => { const ev = S.replay.events; if (n < S.rp.idx) { resetLog(); S.rp.idx = 0; } while (S.rp.idx < n && S.rp.idx < ev.length) appendEvent(ev[S.rp.idx++]); $("#rp-pos").value = S.rp.idx; $("#rp-time").textContent = fmtT(ev[S.rp.idx - 1]?.t_host_us, ev[0]?.t_host_us); renderHeaderFromReplay(); if (instant) renderInspector(); };
  const renderHeaderFromReplay = () => { if (!S.replay) return; const pins = {}; for (const h of S.state.header || []) if (h.kind === "gpio") pins[String(h.bcm)] = { mode: "input", level: null, caps: h.caps || [] }; $("#session-id").textContent = S.replay.id + " (replay)"; for (const e of S.replay.events.slice(0, S.rp.idx)) { if (e.kind === "pin.change") pins[String(e.pin)] = { mode: "input", level: e.level, caps: [] }; if (e.kind === "request.stage" && e.stage === "executed" && e.op === "pin.mode") pins[String(e.args.pin)] = { ...(pins[String(e.args.pin)] || {}), mode: e.args.mode, caps: [] }; if (e.kind === "request.stage" && e.stage === "observed" && e.op === "pin.write") pins[String(e.args.pin)] = { ...(pins[String(e.args.pin)] || { mode: "output" }), level: e.level, caps: [] }; } S.state.pins = pins; S.state.connection.connected = true; renderHeader(); };
  $("#rp-pos").oninput = (ev) => stepReplayTo(+ev.target.value, true);
  $("#rp-speed").onchange = (ev) => (S.rp.speed = +ev.target.value);
  $("#rp-play").onclick = () => {
    if (S.rp.timer) { clearInterval(S.rp.timer); S.rp.timer = null; $("#rp-play").textContent = "▶"; return; }
    if (S.rp.idx >= S.replay.events.length) { resetLog(); S.rp.idx = 0; }
    $("#rp-play").textContent = "❚❚";
    if (S.rp.speed === 0) { stepReplayTo(S.replay.events.length, true); $("#rp-play").textContent = "▶"; return; }
    S.rp.timer = setInterval(() => { const ev = S.replay.events; if (S.rp.idx >= ev.length) { clearInterval(S.rp.timer); S.rp.timer = null; $("#rp-play").textContent = "▶"; return; }
      const now = ev[S.rp.idx - 1]?.t_host_us ?? ev[0].t_host_us; let n = S.rp.idx; while (n < ev.length && (ev[n].t_host_us ?? now) - now <= 50_000 * S.rp.speed) n++; stepReplayTo(Math.max(n, S.rp.idx + 1), false); }, 50);
  };
  $("#replay-exit").onclick = exitReplay;

  // ---------- live feed (SSE) ----------
  const connectFeed = () => {
    const es = new EventSource(`/api/events?cursor=${S.cursor}`);
    es.onmessage = (m) => { const e = JSON.parse(m.data); S.cursor = e.cursor;
      if (S.replay) return;
      if (e.kind === "session.new") { resetLog(); refresh(); loadSessions(); return; }
      if (e.kind === "plan.compiled") return;
      if (e.kind === "replay.start" || e.kind === "replay.stop") return;
      if (e.session && S.state && e.session !== S.state.session.id) return;
      appendEvent(e);
      if (["request.stage", "session.start", "session.end", "control.taken", "control.released", "control.lost", "wiring.request", "wiring.confirmed", "wiring.rejected", "assertion", "plan.end"].includes(e.kind) && (e.stage === undefined || e.stage === "observed")) scheduleRefresh();
    };
    es.onerror = () => { es.close(); setTimeout(connectFeed, 1500); };
  };
  let refreshTimer = null; const scheduleRefresh = () => { if (refreshTimer) return; refreshTimer = setTimeout(async () => { refreshTimer = null; await refresh(); }, 120); };
  setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString(); }, 1000);

  (async () => {
    await refresh(); const cur = await api(`/api/sessions/${S.state.session.id}`); for (const e of cur.events) appendEvent(e);
    S.cursor = S.state.cursor; connectFeed(); loadSessions(); loadPlans(); $("#transport").onchange();
  })();
})();
