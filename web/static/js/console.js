/* Vundu_MIDI console front-end.
   Talks only to the REST API (/api/v1) and the live meter stream (SSE). */
"use strict";

const API = "/api/v1";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) sk.startsWith("--") ? el.style.setProperty(sk, sv) : (el.style[sk] = sv);
    }
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
    el.append(kid instanceof Node ? kid : document.createTextNode(kid));
  return el;
};

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(API + path, opt);
  let data = {};
  try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (!r.ok || data.ok === false) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = "toast", 2600);
}
const fail = (e) => toast(e.message || String(e), true);

/* ------------------------------------------------------------------ */
/* State                                                              */
/* ------------------------------------------------------------------ */
const S = {
  state: null, sig: "", rev: -1, meters: null, wave: null, spectrum: null,
  dragging: 0, learnMode: false, presets: [], sounds: [], effects: null, kbd: false,
  vu: [0, 0], gr: 0, peakHold: [-120, -120], peakHoldT: [0, 0],
};
const controls = new Map(); // target -> [controls] (a target can appear on the desk and in the mixer)
const regCtl = (target, ctl) => { if (!controls.has(target)) controls.set(target, []); controls.get(target).push(ctl); };
const setCtl = (target, v) => (controls.get(target) || []).forEach(c => c.set(v));

/* throttle outgoing parameter changes per key */
const pending = new Map();
function sendThrottled(key, fn, wait = 45) {
  const p = pending.get(key);
  if (p) { p.fn = fn; return; }
  const entry = { fn };
  pending.set(key, entry);
  setTimeout(() => { pending.delete(key); entry.fn().catch(fail); }, wait);
}

/* ------------------------------------------------------------------ */
/* Value formatting                                                   */
/* ------------------------------------------------------------------ */
function fmt(v, spec) {
  if (spec.choices) return spec.choices[Math.round(v)] ?? v;
  const u = spec.unit || "";
  if (spec.text !== undefined && spec.kind === "plugin") return spec.text || (+v).toFixed(2);
  if (u === "Hz") return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k" : Math.round(v) + "";
  if (u === "dB") return (v > 0 ? "+" : "") + (+v).toFixed(Math.abs(v) < 10 ? 1 : 0);
  if (u === "ms") return v >= 100 ? Math.round(v) + "ms" : (+v).toFixed(1) + "ms";
  if (u === ":1") return (+v).toFixed(1) + ":1";
  if (spec.max === 1 && spec.min === 0) return Math.round(v * 100) + "%";
  if (spec.id === "pan") return Math.abs(v) < 0.02 ? "C" : (v < 0 ? "L" : "R") + Math.round(Math.abs(v) * 100);
  return (+v).toFixed(Math.abs(v) < 10 ? 2 : 0);
}

/* ------------------------------------------------------------------ */
/* Knob                                                               */
/* ------------------------------------------------------------------ */
function makeKnob(spec, { color = "#ffb547", onChange, target } = {}) {
  const min = +spec.min, max = +spec.max, step = spec.step;
  let value = +spec.value;
  const dial = h("div", { class: "dial", role: "slider", tabindex: 0, "aria-label": spec.name,
    "aria-valuemin": min, "aria-valuemax": max });
  dial.style.setProperty("--c", color);
  const out = h("output");
  const wrap = h("div", { class: "knob" }, dial, h("label", {}, spec.name), out);
  const norm = () => (value - min) / (max - min || 1);
  const render = () => {
    dial.style.setProperty("--v", norm().toFixed(4));
    dial.setAttribute("aria-valuenow", value);
    out.textContent = fmt(value, spec);
    dial.setAttribute("aria-valuetext", out.textContent);
  };
  const setValue = (v, emit = true) => {
    v = Math.min(max, Math.max(min, v));
    if (step === 1) v = Math.round(v);
    if (v === value) return;
    value = v; render();
    if (emit && onChange) onChange(v);
  };
  let startY = 0, startV = 0, moved = false;
  dial.addEventListener("pointerdown", (e) => {
    if (S.learnMode && target) { learn(target, wrap); return; }
    dial.setPointerCapture(e.pointerId); startY = e.clientY; startV = value; moved = false; S.dragging++;
    wrap.dataset.drag = "1";
  });
  dial.addEventListener("pointermove", (e) => {
    if (!dial.hasPointerCapture(e.pointerId)) return;
    const dy = startY - e.clientY;
    if (Math.abs(dy) > 1) moved = true;
    const range = max - min;
    const sens = e.shiftKey ? 800 : 180;
    setValue(startV + (dy / sens) * range);
  });
  const end = (e) => { if (dial.hasPointerCapture(e.pointerId)) { dial.releasePointerCapture(e.pointerId); S.dragging--; delete wrap.dataset.drag; } };
  dial.addEventListener("pointerup", end);
  dial.addEventListener("pointercancel", end);
  dial.addEventListener("dblclick", () => setValue(+spec.default));
  dial.addEventListener("wheel", (e) => {
    e.preventDefault();
    const inc = step === 1 ? 1 : (max - min) / (e.shiftKey ? 400 : 80);
    setValue(value + (e.deltaY < 0 ? inc : -inc));
  }, { passive: false });
  dial.addEventListener("keydown", (e) => {
    const inc = step === 1 ? 1 : (max - min) / (e.shiftKey ? 200 : 40);
    if (["ArrowUp", "ArrowRight"].includes(e.key)) { setValue(value + inc); e.preventDefault(); }
    if (["ArrowDown", "ArrowLeft"].includes(e.key)) { setValue(value - inc); e.preventDefault(); }
    if (e.key === "Home") setValue(min);
    if (e.key === "End") setValue(max);
  });
  render();
  const ctl = { el: wrap, set: (v) => { if (!wrap.dataset.drag) { value = +v; render(); } }, target };
  if (target) {
    regCtl(target, ctl);
    if (S.state && Object.values(S.state.midi_map || {}).includes(target)) wrap.classList.add("mapped");
  }
  return ctl;
}

/* ------------------------------------------------------------------ */
/* Fader                                                              */
/* ------------------------------------------------------------------ */
function makeFader(spec, { color = "#ffb547", onChange, target, mini = false } = {}) {
  const min = +spec.min, max = +spec.max;
  let value = +spec.value;
  const track = h("div", { class: "track", role: "slider", tabindex: 0, "aria-label": spec.name,
    "aria-valuemin": min, "aria-valuemax": max }, h("div", { class: "slot" }), h("div", { class: "cap" }));
  track.style.setProperty("--c", color);
  const out = h("output");
  const wrap = h("div", { class: "fader" + (mini ? " mini" : "") }, track, h("label", {}, spec.name), out);
  // faders use an audio taper: more resolution near unity
  const toPos = (v) => Math.pow((v - min) / (max - min), 1.6);
  const fromPos = (p) => min + Math.pow(Math.min(1, Math.max(0, p)), 1 / 1.6) * (max - min);
  const render = () => {
    track.style.setProperty("--v", toPos(value).toFixed(4));
    out.textContent = value <= min + 0.01 ? "-∞" : fmt(value, spec);
    track.setAttribute("aria-valuenow", value.toFixed(1));
    track.setAttribute("aria-valuetext", out.textContent);
  };
  const setValue = (v) => {
    v = Math.min(max, Math.max(min, v));
    if (Math.abs(v - value) < 1e-4) return;
    value = v; render(); onChange && onChange(v);
  };
  const posFromEvent = (e) => {
    const r = track.getBoundingClientRect();
    return 1 - (e.clientY - r.top - 6) / (r.height - 12);
  };
  track.addEventListener("pointerdown", (e) => {
    if (S.learnMode && target) { learn(target, wrap); return; }
    track.setPointerCapture(e.pointerId); S.dragging++; wrap.dataset.drag = "1";
    setValue(fromPos(posFromEvent(e)));
  });
  track.addEventListener("pointermove", (e) => {
    if (track.hasPointerCapture(e.pointerId)) setValue(fromPos(posFromEvent(e)));
  });
  const end = (e) => { if (track.hasPointerCapture(e.pointerId)) { track.releasePointerCapture(e.pointerId); S.dragging--; delete wrap.dataset.drag; } };
  track.addEventListener("pointerup", end);
  track.addEventListener("pointercancel", end);
  track.addEventListener("dblclick", () => setValue(+spec.default));
  track.addEventListener("wheel", (e) => { e.preventDefault(); setValue(value + (e.deltaY < 0 ? 0.5 : -0.5)); }, { passive: false });
  track.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp") { setValue(value + 0.5); e.preventDefault(); }
    if (e.key === "ArrowDown") { setValue(value - 0.5); e.preventDefault(); }
  });
  render();
  const ctl = { el: wrap, set: (v) => { if (!wrap.dataset.drag) { value = +v; render(); } }, target };
  if (target) regCtl(target, ctl);
  return ctl;
}

function lamp(text, on, onToggle, cls = "") {
  const b = h("button", { class: "lamp " + cls, "aria-pressed": on ? "true" : "false" }, text);
  b.addEventListener("click", () => {
    const nv = b.getAttribute("aria-pressed") !== "true";
    b.setAttribute("aria-pressed", nv); onToggle(nv);
  });
  return b;
}

/* ------------------------------------------------------------------ */
/* MIDI learn                                                         */
/* ------------------------------------------------------------------ */
async function learn(target, el) {
  $$(".knob.learn, .fader.learn").forEach(k => k.classList.remove("learn"));
  el.classList.add("learn");
  try { await api("POST", "/midi/learn", { target }); toast(`Move a knob or slider on your MIDI device to map ${target}`); }
  catch (e) { fail(e); }
}

/* ------------------------------------------------------------------ */
/* Desk rendering                                                     */
/* ------------------------------------------------------------------ */
function signature(st) {
  return JSON.stringify([st.instrument, st.guitar_source, st.sound, st.chain.map(c => [c.slot_id, c.enabled, c.params.length]),
    st.master.comp.enabled, st.master.eq.enabled, st.master.mute, st.master.limiter,
    st.master.inst_mute, st.master.input_mute, st.master.fx_mute, st.master.playback_mute, Object.keys(st.midi_map || {}).length, st.learn]);
}

async function refreshState(force = false) {
  try {
    const st = await api("GET", "/state");
    S.state = st; S.rev = st.rev;
    const sig = signature(st);
    if (force || sig !== S.sig) { S.sig = sig; renderAll(); }
    else updateValues();
    renderScreenInfo();
  } catch (e) { fail(e); }
}

let refreshTimer;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => { if (S.dragging) scheduleRefresh(); else refreshState(); }, 250);
}

function renderAll() {
  controls.clear();
  renderMaster();
  renderMixer();
  renderStrips();
  renderDeckMeta();
}

/* ------------------------------------------------------------------ */
/* Mini mixer                                                         */
/* ------------------------------------------------------------------ */
const MIXER = [
  { id: "inst", name: "Keys", level: "instrument_level", mute: "inst_mute", range: [-60, 6, 0], color: "#f2ead3", tip: "MIDI instrument level" },
  { id: "input", name: "Guitar", level: "input_gain", mute: "input_mute", range: [-24, 24, 0], color: "#ffd43b", tip: "Guitar / audio input gain" },
  { id: "fx", name: "Effects", level: "fx_return", mute: "fx_mute", range: [-60, 12, 0], color: "#4dabf7", tip: "Effects chain return. Mute bypasses every effect" },
  { id: "play", name: "Playback", level: "playback_level", mute: "playback_mute", range: [-60, 6, 0], color: "#38d9a9", tip: "Tape deck playback level" },
  { id: "master", name: "Main", level: "volume", mute: "mute", range: [-60, 6, -6], color: "#ffb547", tip: "Main output" },
];
function renderMixer() {
  const st = S.state, m = st.master;
  const midiMode = st.instrument === "keyboard" || st.guitar_source === "midi";
  const box = $("#mixer");
  box.replaceChildren();
  MIXER.forEach(c => {
    const idle = (c.id === "inst" && !midiMode) || (c.id === "input" && midiMode);
    const meter = h("div", { class: "mch-meter", "aria-hidden": "true" }, h("i", { "data-ch": c.id }));
    const fader = makeFader({ id: c.level, name: c.name, min: c.range[0], max: c.range[1], default: c.range[2], unit: "dB", value: m[c.level] }, {
      color: c.color, mini: true, target: `master.${c.level}`,
      onChange: (v) => sendThrottled("mx" + c.level, () => api("PATCH", "/master", { [c.level]: v })),
    });
    box.append(h("div", { class: "mch" + (idle ? " idle" : ""), title: idle ? `${c.tip} (not used by the current instrument)` : c.tip },
      h("span", { class: "mch-name" }, c.name),
      lamp("Mute", m[c.mute], (v) => api("PATCH", "/master", { [c.mute]: v }).then(() => refreshState()).catch(fail), "red"),
      h("div", { class: "mch-body" }, meter, fader.el)));
  });
  requestAnimationFrame(() => $$(".mch-meter").forEach(el => el.style.setProperty("--full", el.clientHeight + "px")));
}

function updateValues() {
  const st = S.state;
  st.chain.forEach((slot) => slot.params.forEach(p => setCtl(`chain.${slot.index}.${p.id}`, p.value)));
  st.master.comp.params.forEach(p => setCtl(`master.comp.${p.id}`, p.value));
  st.master.eq.params.forEach(p => setCtl(`master.eq.${p.id}`, p.value));
  for (const k of ["volume", "pan", "instrument_level", "input_gain", "fx_return", "playback_level"]) setCtl(`master.${k}`, st.master[k]);
}

function renderMaster() {
  const m = S.state.master;
  const comp = $("#comp-knobs"), eq = $("#eq-knobs");
  comp.replaceChildren(); eq.replaceChildren();
  const compShown = ["threshold", "ratio", "attack", "release", "knee", "makeup"];
  m.comp.params.filter(p => compShown.includes(p.id)).forEach(p => comp.append(makeKnob(p, {
    color: "#ff6b6b", target: `master.comp.${p.id}`,
    onChange: (v) => sendThrottled("mc" + p.id, () => api("PATCH", "/master", { comp: { params: { [p.id]: v } } })),
  }).el));
  m.eq.params.forEach(p => eq.append(makeKnob(p, {
    color: "#69db7c", target: `master.eq.${p.id}`,
    onChange: (v) => sendThrottled("me" + p.id, () => api("PATCH", "/master", { eq: { params: { [p.id]: v } } })),
  }).el));
  const compBtn = $("#comp-on"), eqBtn = $("#eq-on");
  compBtn.setAttribute("aria-pressed", m.comp.enabled);
  eqBtn.setAttribute("aria-pressed", m.eq.enabled);
  compBtn.onclick = () => api("PATCH", "/master", { comp: { enabled: compBtn.getAttribute("aria-pressed") !== "true" } }).then(() => refreshState()).catch(fail);
  eqBtn.onclick = () => api("PATCH", "/master", { eq: { enabled: eqBtn.getAttribute("aria-pressed") !== "true" } }).then(() => refreshState()).catch(fail);
}

function seg(options, current, onPick) {
  const wrap = h("div", { class: "seg", role: "group" });
  options.forEach(([val, label]) => wrap.append(h("button", {
    "aria-pressed": val === current ? "true" : "false",
    onclick: () => onPick(val),
  }, label)));
  return wrap;
}

function renderStrips() {
  const st = S.state;
  const box = $("#strips");
  box.replaceChildren();

  /* --- instrument strip --- */
  const midiMode = st.instrument === "keyboard" || st.guitar_source === "midi";
  const inst = h("div", { class: "strip", style: { "--c": "#f2ead3" } },
    h("div", { class: "strip-cap" }),
    h("h3", {}, st.instrument === "guitar" ? "Guitar" : "Keyboard"),
    h("span", { class: "kind" }, midiMode ? "MIDI in" : "Audio in"),
    seg([["keyboard", "Keys"], ["guitar", "Guitar"]], st.instrument,
      (v) => api("POST", "/instrument", { instrument: v }).then(() => refreshState(true)).catch(fail)),
  );
  if (st.instrument === "guitar") {
    inst.append(seg([["audio", "Jack"], ["midi", "MIDI"]], st.guitar_source,
      (v) => api("POST", "/instrument", { guitar_source: v }).then(() => refreshState(true)).catch(fail)));
  }
  if (midiMode) {
    const sel = h("select", { "aria-label": "Sound",
      onchange: (e) => api("POST", "/instrument", { sound: e.target.value }).then(() => { toast("Sound: " + e.target.selectedOptions[0].text); refreshState(); }).catch(fail) });
    const groups = {};
    S.sounds.forEach(s => (groups[s.group] = groups[s.group] || []).push(s));
    for (const [g, items] of Object.entries(groups)) {
      const og = h("optgroup", { label: g });
      items.forEach(s => og.append(h("option", { value: s.id, selected: s.id === st.sound ? "selected" : null }, s.name)));
      sel.append(og);
    }
    inst.append(sel);
  }
  inst.append(h("div", { class: "spacer" }));
  const levelKey = midiMode ? "instrument_level" : "input_gain";
  const range = midiMode ? [-60, 6, 0] : [-24, 24, 0];
  inst.append(makeFader({ id: levelKey, name: midiMode ? "Level" : "Input gain", min: range[0], max: range[1],
    default: range[2], unit: "dB", value: st.master[levelKey] }, {
    color: "#f2ead3", target: `master.${levelKey}`,
    onChange: (v) => sendThrottled(levelKey, () => api("PATCH", "/master", { [levelKey]: v })),
  }).el);
  box.append(inst);

  /* --- effect strips --- */
  st.chain.forEach((slot) => box.append(effectStrip(slot)));

  /* --- add slot --- */
  const max = st.max_slots || 20, used = st.chain.length, full = used >= max;
  box.append(h("div", { class: "strip add" },
    h("button", { "aria-label": "Add effect", title: full ? "The chain is full" : "Add effect", disabled: full ? true : null,
      onclick: () => openPanel("plugins") }, "+"),
    h("span", { class: "kind" }, full ? "Chain full" : "Add effect"),
    h("span", { class: "kind" }, `${used} of ${max} effects`)));

  /* --- master strip --- */
  const m = st.master;
  const master = h("div", { class: "strip master", style: { "--c": "#ffb547" } },
    h("div", { class: "strip-cap" }),
    h("h3", {}, "Master"),
    h("span", { class: "kind" }, "Main out"),
    makeKnob({ id: "pan", name: "Pan", min: -1, max: 1, default: 0, value: m.pan }, {
      target: "master.pan", onChange: (v) => sendThrottled("pan", () => api("PATCH", "/master", { pan: v })) }).el,
    h("div", { class: "row" },
      lamp("Mute", m.mute, (v) => api("PATCH", "/master", { mute: v }).catch(fail), "red"),
      lamp("Limit", m.limiter, (v) => api("PATCH", "/master", { limiter: v }).catch(fail), "green")),
    h("div", { class: "spacer" }),
    makeFader({ id: "volume", name: "Main", min: -60, max: 6, default: -6, unit: "dB", value: m.volume }, {
      target: "master.volume", onChange: (v) => sendThrottled("vol", () => api("PATCH", "/master", { volume: v })) }).el,
  );
  box.append(master);
}

function effectStrip(slot) {
  const color = slot.color || "#ffb547";
  const strip = h("div", { class: "strip" + (slot.enabled ? "" : " bypassed"), style: { "--c": color }, "data-slot": slot.slot_id });
  const isPlugin = slot.kind === "plugin";
  const mixParam = slot.params.find(p => p.id === "mix");
  const knobParams = slot.params.filter(p => p !== mixParam).slice(0, isPlugin ? 6 : 8);
  strip.append(
    h("div", { class: "strip-cap" }),
    h("h3", { title: slot.name }, slot.name),
    h("span", { class: "kind" }, isPlugin ? `${slot.format || "Plugin"} · ${slot.num_params} params` : slot.category),
    h("div", { class: "row" },
      lamp("On", slot.enabled, (v) => api("PATCH", `/chain/${slot.slot_id}`, { enabled: v }).then(() => refreshState()).catch(fail), "green")),
    h("span", { class: "gr-mini", "data-gr": slot.slot_id }),
  );
  const knobs = h("div", { class: "knobs" });
  knobParams.forEach(p => knobs.append(makeKnob({ ...p, kind: slot.kind }, {
    color, target: `chain.${slot.index}.${p.id}`,
    onChange: (v) => sendThrottled(`${slot.slot_id}.${p.id}`, () => api("PATCH", `/chain/${slot.slot_id}`, { params: { [p.id]: v } })),
  }).el));
  strip.append(knobs, h("div", { class: "spacer" }));
  if (mixParam) {
    strip.append(makeFader({ ...mixParam, name: "Mix", min: 0, max: 1, unit: "" }, {
      color, target: `chain.${slot.index}.mix`,
      onChange: (v) => sendThrottled(`${slot.slot_id}.mix`, () => api("PATCH", `/chain/${slot.slot_id}`, { params: { mix: v } })),
    }).el);
  }
  const n = S.state.chain.length;
  strip.append(h("div", { class: "strip-tools" },
    h("button", { title: "Move left", "aria-label": "Move left", disabled: slot.index === 0 ? true : null,
      onclick: () => api("POST", `/chain/${slot.slot_id}/move`, { position: slot.index - 1 }).then(() => refreshState()).catch(fail) }, "◀"),
    isPlugin ? h("button", { title: "All parameters", onclick: () => openPluginEditor(slot.slot_id) }, "Edit") : null,
    h("button", { title: "Remove", "aria-label": "Remove effect",
      onclick: () => api("DELETE", `/chain/${slot.slot_id}`).then(() => { toast(`${slot.name} removed`); refreshState(); }).catch(fail) }, "✕"),
    h("button", { title: "Move right", "aria-label": "Move right", disabled: slot.index === n - 1 ? true : null,
      onclick: () => api("POST", `/chain/${slot.slot_id}/move`, { position: slot.index + 1 }).then(() => refreshState()).catch(fail) }, "▶"),
  ));
  return strip;
}

/* ------------------------------------------------------------------ */
/* Screen                                                             */
/* ------------------------------------------------------------------ */
const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const noteName = (n) => n === null || n === undefined ? "—" : `${NOTE_NAMES[n % 12]}${Math.floor(n / 12) - 1}`;

function renderScreenInfo() {
  const st = S.state; if (!st) return;
  $("#si-preset").textContent = st.preset?.name || "Init";
  const inst = st.instrument === "guitar" ? (st.guitar_source === "midi" ? "Guitar (MIDI)" : "Guitar (jack)") : "Keyboard";
  $("#si-inst").textContent = inst;
  $("#si-sound").textContent = st.instrument === "guitar" && st.guitar_source === "audio" ? "Live input" : st.sound_name;
  $("#si-midi").textContent = st.midi.port || "not connected";
  $("#si-engine").textContent = `${st.audio.sample_rate / 1000}k / ${st.audio.block_size} · ${st.audio.latency_ms}ms`;
  const chain = $("#si-chain"); chain.replaceChildren(...st.chain.map(c => h("span", { class: c.enabled ? "" : "off", style: { color: c.color } }, c.name)));
  $("#led-audio").className = "led-tag " + (st.audio.backend.startsWith("sounddevice") ? "on" : "warn");
  $("#led-audio").title = `Audio: ${st.audio.backend}${st.audio.error ? " (" + st.audio.error + ")" : ""}`;
  $("#led-midi").className = "led-tag " + (st.midi.connected ? "on" : "");
  $("#led-midi").title = st.midi.connected ? `MIDI: ${st.midi.port}` : "No MIDI interface connected";
  $("#led-plug").className = "led-tag " + (st.minihost ? "on" : "warn");
  $("#led-plug").title = st.minihost ? "Minihost plugin host ready" : "Minihost not installed: built-in effects only";
}

/* ------------------------------------------------------------------ */
/* Meters + canvases                                                  */
/* ------------------------------------------------------------------ */
function fitCanvas(c) {
  const r = c.getBoundingClientRect(), d = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width * d)), hh = Math.max(1, Math.round(r.height * d));
  if (c.width !== w || c.height !== hh) { c.width = w; c.height = hh; }
  return [c.getContext("2d"), w, hh, d];
}

const VU_MARKS = [-20, -10, -7, -5, -3, -2, -1, 0, 1, 2, 3];
const vuPos = (vu) => { // real VU scales are linear in voltage
  const lo = Math.pow(10, -20 / 20), hi = Math.pow(10, 3 / 20);
  return (Math.pow(10, Math.max(-25, Math.min(4, vu)) / 20) - lo) / (hi - lo);
};
function drawVU(canvas, vu, label = "VU", grMode = false) {
  const [g, w, hh, d] = fitCanvas(canvas);
  const cx = w / 2, cy = hh * 1.05, R = hh * 0.82;
  const a0 = -Math.PI / 2 - 0.85, a1 = -Math.PI / 2 + 0.85;
  // warm back-light
  const bg = g.createRadialGradient(cx, hh * 0.35, 10, cx, hh * 0.5, w * 0.7);
  bg.addColorStop(0, "#fff6d6"); bg.addColorStop(1, "#e3cf98");
  g.fillStyle = bg; g.fillRect(0, 0, w, hh);
  g.lineCap = "round";
  // red zone
  const posToAng = (p) => a0 + (a1 - a0) * p;
  if (!grMode) {
    g.strokeStyle = "#c8312b"; g.lineWidth = 5 * d;
    g.beginPath(); g.arc(cx, cy, R, posToAng(vuPos(0)), posToAng(1)); g.stroke();
  }
  g.strokeStyle = "#2a2118"; g.lineWidth = 1.4 * d;
  g.beginPath(); g.arc(cx, cy, R, a0, posToAng(grMode ? 1 : vuPos(0))); g.stroke();
  g.font = `${11 * d}px "Barlow Condensed", sans-serif`; g.textAlign = "center"; g.textBaseline = "middle";
  const marks = grMode ? [0, -2, -4, -6, -10, -15, -20] : VU_MARKS;
  marks.forEach((m) => {
    const p = grMode ? 1 - (-m / 20) : vuPos(m);
    const a = posToAng(p);
    const major = grMode || [-20, -10, -7, -5, -3, 0, 3].includes(m);
    g.strokeStyle = !grMode && m > 0 ? "#c8312b" : "#2a2118";
    g.lineWidth = (major ? 1.8 : 1.1) * d;
    g.beginPath();
    g.moveTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    g.lineTo(cx + Math.cos(a) * (R + (major ? 9 : 5) * d), cy + Math.sin(a) * (R + (major ? 9 : 5) * d));
    g.stroke();
    if (major) {
      g.fillStyle = !grMode && m > 0 ? "#c8312b" : "#2a2118";
      g.fillText(grMode ? String(m) : (m > 0 ? "+" + m : String(m)), cx + Math.cos(a) * (R + 19 * d), cy + Math.sin(a) * (R + 19 * d));
    }
  });
  g.fillStyle = "#2a2118"; g.font = `600 ${15 * d}px "Barlow Condensed", sans-serif`;
  g.fillText(label, cx, hh * 0.68);
  // needle
  const p = grMode ? 1 - Math.min(20, -vu) / 20 : vuPos(vu);
  const a = posToAng(Math.max(-0.03, Math.min(1.04, p)));
  g.strokeStyle = "#141110"; g.lineWidth = 1.6 * d;
  g.beginPath(); g.moveTo(cx, cy); g.lineTo(cx + Math.cos(a) * (R + 6 * d), cy + Math.sin(a) * (R + 6 * d)); g.stroke();
  // pivot cover
  g.fillStyle = "#1d1a17"; g.beginPath(); g.arc(cx, hh, hh * 0.16, Math.PI, 0); g.fill();
  // glass shine
  const sh = g.createLinearGradient(0, 0, w, hh); sh.addColorStop(0, "rgba(255,255,255,.35)"); sh.addColorStop(.4, "rgba(255,255,255,0)");
  g.fillStyle = sh; g.fillRect(0, 0, w, hh);
}

function drawLedMeter(canvas, peaks, holds) {
  const [g, w, hh, d] = fitCanvas(canvas);
  g.clearRect(0, 0, w, hh);
  const segs = 30, gap = 2 * d, top = 8 * d, bottom = 8 * d;
  const segH = (hh - top - bottom - gap * (segs - 1)) / segs;
  const colW = w * 0.28;
  const cols = [w * 0.12, w * 0.6];
  const dbAt = (i) => -60 + (i + 1) * 2; // 2 dB steps, -60..0
  g.font = `${9 * d}px "Barlow Condensed", sans-serif`; g.fillStyle = "#8f8a7c"; g.textAlign = "center";
  [0, -6, -12, -18, -30, -48].forEach(db => {
    const i = (db + 60) / 2 - 1;
    const y = hh - bottom - (i + 1) * (segH + gap) + segH / 2 + 3 * d;
    g.fillText(db, w * 0.5, y);
  });
  peaks.forEach((pk, c) => {
    for (let i = 0; i < segs; i++) {
      const db = dbAt(i);
      const y = hh - bottom - (i + 1) * (segH + gap) + gap;
      const lit = pk >= db - 2, hold = Math.abs(holds[c] - db) < 1.01;
      const col = db > -3 ? "#ff4b3e" : db > -12 ? "#ffb547" : "#5cff8a";
      g.fillStyle = lit || hold ? col : "rgba(255,255,255,.06)";
      g.shadowColor = col; g.shadowBlur = lit ? 6 * d : 0;
      g.fillRect(cols[c], y, colW, segH);
    }
  });
  g.shadowBlur = 0;
}

function drawWave(canvas) {
  const [g, w, hh, d] = fitCanvas(canvas);
  g.clearRect(0, 0, w, hh);
  const mid = hh * 0.45;
  g.strokeStyle = "rgba(127,233,255,.12)"; g.lineWidth = 1;
  for (let i = 1; i < 4; i++) { g.beginPath(); g.moveTo(0, hh * 0.9 * i / 4); g.lineTo(w, hh * 0.9 * i / 4); g.stroke(); }
  const wv = S.wave;
  if (!wv) return;
  const ov = wv.overview || [];
  const bw = w / Math.max(ov.length, 1);
  const amp = hh * 0.4;
  ov.forEach(([lo, hi, rec], i) => {
    g.fillStyle = rec ? "rgba(255,90,80,.55)" : "rgba(127,233,255,.38)";
    const y0 = mid - Math.min(1, hi) * amp, y1 = mid - Math.max(-1, lo) * amp;
    g.fillRect(i * bw, y0, Math.max(1, bw - 0.5), Math.max(1, y1 - y0));
  });
  const sc = wv.scope || [];
  if (sc.length) {
    g.strokeStyle = "#ffb547"; g.lineWidth = 1.5 * d; g.shadowColor = "#ffb547"; g.shadowBlur = 6 * d;
    g.beginPath();
    sc.forEach((v, i) => { const x = (i / (sc.length - 1)) * w, y = mid - Math.max(-1, Math.min(1, v * 2.5)) * amp; i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke(); g.shadowBlur = 0;
  }
}

function drawSpectrum(canvas) {
  const [g, w, hh, d] = fitCanvas(canvas);
  g.clearRect(0, 0, w, hh);
  const sp = S.spectrum; if (!sp) return;
  const n = sp.length, bw = (w - 20 * d) / n;
  const grad = g.createLinearGradient(0, hh, 0, 0);
  grad.addColorStop(0, "rgba(127,233,255,.25)"); grad.addColorStop(.7, "rgba(127,233,255,.9)"); grad.addColorStop(1, "#ffb547");
  g.fillStyle = grad;
  sp.forEach((db, i) => {
    const v = Math.max(0, (db + 84) / 84);
    const bh = v * (hh - 40 * d);
    g.fillRect(10 * d + i * bw + 1, hh - 16 * d - bh, bw - 2, bh);
  });
  g.fillStyle = "rgba(127,233,255,.5)"; g.font = `${10 * d}px "Share Tech Mono", monospace`;
  g.fillText("30", 10 * d, hh - 4 * d); g.fillText("1k", w * 0.52, hh - 4 * d); g.fillText("18k", w - 30 * d, hh - 4 * d);
}

function frame() {
  const m = S.meters;
  if (m) {
    // VU ballistics: ~300 ms integration, 0 VU = -18 dBFS
    for (let c = 0; c < 2; c++) {
      const target = m.out_rms[c] + 18;
      S.vu[c] += (target - S.vu[c]) * 0.12;
      const now = performance.now();
      if (m.out_peak[c] >= S.peakHold[c] || now - S.peakHoldT[c] > 1500) { S.peakHold[c] = m.out_peak[c]; S.peakHoldT[c] = now; }
    }
    S.gr += (m.gr - S.gr) * 0.25;
  }
  drawVU($("#vu-l"), S.vu[0]);
  drawVU($("#vu-r"), S.vu[1]);
  drawVU($("#vu-gr"), S.gr, "GR dB", true);
  drawLedMeter($("#led-meter"), m ? m.out_peak : [-120, -120], S.peakHold.map(v => Math.round(v / 2) * 2));
  drawWave($("#wave"));
  drawSpectrum($("#spectrum"));
  requestAnimationFrame(frame);
}

function onMeters(data) {
  const m = data.meters;
  S.meters = m; S.wave = data.wave; S.spectrum = data.spectrum;
  $("#db-l").textContent = m.out_peak[0] < -90 ? "-∞" : m.out_peak[0].toFixed(1);
  $("#db-r").textContent = m.out_peak[1] < -90 ? "-∞" : m.out_peak[1].toFixed(1);
  $("#clip-led").classList.toggle("on", m.clip);
  $("#si-cpu").textContent = `${m.cpu.toFixed(0)}%${m.xruns ? ` · ${m.xruns} dropouts` : ""}`;
  $("#si-note").textContent = m.notes.length ? m.notes.map(noteName).join(" ") : noteName(m.last_note);
  const rc = $("#rec-clock");
  rc.textContent = clock(m.deck.position);
  rc.classList.toggle("live", m.rec.active);
  for (const [id, db] of Object.entries(m.ch || {})) {
    const el = $(`.mch-meter i[data-ch="${id}"]`);
    if (el) el.style.height = Math.max(0, Math.min(100, (db + 60) / 60 * 100)).toFixed(1) + "%";
  }
  updateDeck(m.deck);
  for (const [id, gr] of Object.entries(m.slots_gr || {})) {
    const el = $(`[data-gr="${id}"]`); if (el) el.textContent = gr < -0.5 ? `GR ${gr.toFixed(1)} dB` : "";
  }
  if (m.rev !== S.rev) { S.rev = m.rev; scheduleRefresh(); }
}

function connectStream() {
  if (!window.EventSource) return pollMeters();
  const es = new EventSource(API + "/stream?fps=20");
  es.onmessage = (e) => { try { onMeters(JSON.parse(e.data)); } catch (_) { } };
  es.onerror = () => { es.close(); setTimeout(connectStream, 2000); };
}
async function pollMeters() {
  try {
    const [m, w, s] = await Promise.all([api("GET", "/meters"), api("GET", "/waveform?points=240"), api("GET", "/spectrum?bands=40")]);
    onMeters({ meters: m, wave: w, spectrum: s.bands });
  } catch (_) { }
  setTimeout(pollMeters, 80);
}

/* ------------------------------------------------------------------ */
/* Drawer panels                                                      */
/* ------------------------------------------------------------------ */
const PANELS = {
  plugins: { title: "Plugins", render: panelPlugins },
  library: { title: "Library", render: panelLibrary },
  presets: { title: "Presets", render: panelPresets },
  instruments: { title: "Instruments", render: panelInstruments },
  devices: { title: "Devices", render: panelDevices },
  recordings: { title: "Recordings", render: panelRecordings },
  midimap: { title: "MIDI map", render: panelMidiMap },
  settings: { title: "Settings", render: panelSettings },
  about: { title: "Help", render: panelAbout },
};
let currentPanel = null;

async function openPanel(name) {
  const p = PANELS[name]; if (!p) return;
  currentPanel = name;
  $$(".menu button").forEach(b => b.setAttribute("aria-expanded", b.dataset.panel === name ? "true" : "false"));
  $("#drawer-title").textContent = p.title;
  const body = $("#drawer-body");
  body.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false"); $("#scrim").hidden = false;
  try { body.replaceChildren(...[].concat(await p.render())); } catch (e) { body.replaceChildren(h("p", {}, "Could not load: " + e.message)); }
  $("#drawer-close").focus();
}
function closePanel(keepLearn = false) {
  currentPanel = null;
  $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); $("#scrim").hidden = true;
  $$(".menu button").forEach(b => b.setAttribute("aria-expanded", "false"));
  if (S.learnMode && keepLearn !== true) setLearn(false);
}
const rerender = () => currentPanel && openPanel(currentPanel);

const item = ({ title, sub, color, active, onclick, right }) => h(onclick ? "button" : "div", { class: "item" + (active ? " active" : ""), onclick },
  color ? h("span", { class: "swatch", style: { background: color } }) : null,
  h("span", { class: "grow" }, h("span", { class: "t" }, title), sub ? h("span", { class: "s" }, sub) : null),
  right || null);

async function addEffect(spec, label) {
  try { await api("POST", "/chain", spec); toast(`${label} added to the chain`); await refreshState(); }
  catch (e) { fail(e); }
}

async function panelPlugins() {
  const [fx, pl] = await Promise.all([api("GET", "/effects"), api("GET", "/plugins")]);
  const used = S.state?.chain.length || 0, max = S.state?.max_slots || 20;
  const out = [h("p", {}, `Pick an effect to add it to the end of the chain. Each effect becomes a channel strip on the desk. ${used} of ${max} slots used.`)];
  out.push(h("h3", {}, "Built-in effects"));
  const cats = {};
  fx.builtin.forEach(e => (cats[e.category] = cats[e.category] || []).push(e));
  const list = h("div", { class: "list" });
  for (const [cat, items] of Object.entries(cats)) items.forEach(e => list.append(item({
    title: e.name, sub: `${cat} — ${e.description}`, color: e.color, onclick: () => addEffect({ type: e.type }, e.name) })));
  out.push(list);
  out.push(h("h3", {}, "Plugins (VST3, LV2, CLAP, AU) via Minihost"));
  if (!pl.available) {
    out.push(h("p", {}, "Minihost is not installed, so only the built-in effects are available."),
      h("pre", { class: "code" }, "pip install minihost\n# Raspberry Pi 5: see README, 'Plugins on the Pi'"));
    return out;
  }
  const scanInfo = h("span", { class: "muted" }, pl.scan.running ? `Scanning… ${pl.scan.done}/${pl.scan.total}` : `${pl.plugins.length} plugins found`);
  out.push(h("div", { class: "row" },
    h("button", { class: "btn primary", onclick: async () => { try { await api("POST", "/plugins/scan", {}); toast("Scanning for plugins…"); watchScan(scanInfo); } catch (e) { fail(e); } } }, "Scan for plugins"),
    h("button", { class: "btn", onclick: async () => { try { await api("POST", "/plugins/scan", { refresh: true }); watchScan(scanInfo); } catch (e) { fail(e); } } }, "Rescan all"),
    scanInfo));
  const effects = pl.plugins.filter(p => !p.is_instrument);
  const plist = h("div", { class: "list" });
  effects.forEach(p => plist.append(item({ title: p.name, sub: `${p.format} — ${p.vendor || "unknown vendor"}`, color: "#ff922b",
    onclick: () => addEffect({ type: "plugin", plugin: p.id }, p.name) })));
  if (!effects.length) plist.append(h("p", { class: "muted" }, "No effect plugins yet. Install some (for example LV2 packs from your package manager) and press Scan."));
  out.push(plist);
  out.push(h("h3", {}, "Folders searched"), h("pre", { class: "code" }, pl.dirs.join("\n")));
  return out;
}

function watchScan(el) {
  const tick = async () => {
    try {
      const r = await api("GET", "/plugins/scan");
      el.textContent = r.scan.running ? `Scanning… ${r.scan.done}/${r.scan.total}` : `${r.count} plugins found`;
      if (r.scan.running) setTimeout(tick, 700);
      else { await loadSounds(); if (currentPanel === "plugins") rerender(); }
    } catch (_) { }
  };
  tick();
}

async function panelLibrary() {
  const [snd, pre, rec] = await Promise.all([api("GET", "/sounds"), api("GET", "/presets"), api("GET", "/recordings")]);
  const out = [h("p", {}, "Everything Vundu_MIDI can play: sounds for MIDI instruments, presets and your recordings.")];
  out.push(h("h3", {}, `Sounds (${snd.sounds.length})`));
  const l1 = h("div", { class: "list" });
  snd.sounds.forEach(s => l1.append(item({ title: s.name, sub: s.group, active: s.id === snd.current,
    onclick: () => api("POST", "/instrument", { sound: s.id }).then(() => { toast("Sound: " + s.name); refreshState(); rerender(); }).catch(fail) })));
  out.push(l1, h("h3", {}, `Presets (${pre.presets.length})`), h("p", { class: "muted" }, "Open Presets to load or save them."));
  out.push(h("h3", {}, `Recordings (${rec.recordings.length})`), h("p", { class: "muted" }, "Open Recordings to listen, download or delete."));
  return out;
}

async function loadPresets() { const r = await api("GET", "/presets"); S.presets = r.presets; return r; }

async function loadPreset(id) {
  try {
    const r = await api("POST", "/presets/load", { id });
    toast(`Preset: ${r.preset.name}` + (r.errors.length ? ` (skipped: ${r.errors.join("; ")})` : ""), r.errors.length > 0);
    await refreshState(true);
  } catch (e) { fail(e); }
}

async function panelPresets() {
  const r = await loadPresets();
  const out = [];
  const name = h("input", { id: "pname", placeholder: "My guitar sound", value: "" });
  const cat = h("input", { id: "pcat", placeholder: "Guitar", value: "User" });
  out.push(h("h3", {}, "Save current settings"),
    h("div", { class: "field" }, h("label", { for: "pname" }, "Name"), name),
    h("div", { class: "field" }, h("label", { for: "pcat" }, "Category"), cat),
    h("button", { class: "btn primary", onclick: async () => {
      try { const s = await api("POST", "/presets", { name: name.value, category: cat.value }); toast(`Saved ${s.preset.name}`); rerender(); refreshState(); } catch (e) { fail(e); }
    } }, "Save preset"));
  const cats = {};
  r.presets.forEach(p => (cats[p.category] = cats[p.category] || []).push(p));
  for (const [c, items] of Object.entries(cats)) {
    out.push(h("h3", {}, c));
    const l = h("div", { class: "list" });
    items.forEach(p => l.append(item({ title: p.name, sub: p.description || p.effects.join(", "), active: r.current?.id === p.id,
      onclick: () => { loadPreset(p.id).then(rerender); },
      right: p.origin === "user" ? h("button", { class: "btn small danger", onclick: (e) => {
        e.stopPropagation();
        if (!confirm(`Delete preset "${p.name}"? This cannot be undone.`)) return;
        api("DELETE", `/presets/${encodeURIComponent(p.id)}`).then(() => { toast("Preset deleted"); rerender(); }).catch(fail);
      } }, "Delete") : h("span", { class: "badge" }, "factory") })));
    out.push(l);
  }
  out.push(h("p", { class: "muted" }, "Tip: a MIDI Program Change message selects presets by number, in this order."));
  return out;
}

async function stepPreset(dir) {
  if (!S.presets.length) await loadPresets();
  const idx = S.presets.findIndex(p => p.id === S.state?.preset?.id);
  const next = S.presets[(idx + dir + S.presets.length) % S.presets.length];
  if (next) loadPreset(next.id);
}

function piano() {
  const el = h("div", { class: "piano", "aria-label": "On-screen keyboard" });
  const base = 48, whites = [0, 2, 4, 5, 7, 9, 11], blacks = { 1: 0, 3: 1, 6: 3, 8: 4, 10: 5 };
  const octaves = 2, wCount = 7 * octaves + 1;
  const keyFor = {};
  for (let o = 0; o < octaves; o++) whites.forEach((s, i) => { const k = h("div", { class: "w" }); k.dataset.note = base + o * 12 + s; el.append(k); keyFor[base + o * 12 + s] = k; });
  const top = h("div", { class: "w" }); top.dataset.note = base + octaves * 12; el.append(top);
  for (let o = 0; o < octaves; o++) for (const [s, wi] of Object.entries(blacks)) {
    const k = h("div", { class: "b", style: { left: `calc(${((o * 7 + wi + 1) / wCount) * 100}% - 3%)` } });
    k.dataset.note = base + o * 12 + +s; el.append(k);
  }
  const down = (e) => { const n = e.target.dataset.note; if (!n) return; e.target.classList.add("on"); noteOn(+n); e.target.setPointerCapture?.(e.pointerId); };
  const up = (e) => { const n = e.target.dataset.note; if (!n) return; e.target.classList.remove("on"); noteOff(+n); };
  el.addEventListener("pointerdown", down); el.addEventListener("pointerup", up); el.addEventListener("pointercancel", up);
  return el;
}

const noteOn = (n, v = 100) => api("POST", "/midi/send", { type: "note_on", note: n, velocity: v }).catch(fail);
const noteOff = (n) => api("POST", "/midi/send", { type: "note_off", note: n }).catch(fail);

async function panelInstruments() {
  const [ins, snd] = await Promise.all([api("GET", "/instruments"), api("GET", "/sounds")]);
  const out = [h("p", {}, "Choose what is plugged in. Then pick a sound (for MIDI) and an effect preset.")];
  const l = h("div", { class: "list" });
  ins.instruments.forEach(i => l.append(item({ title: i.name, sub: i.description, active: i.id === ins.current,
    onclick: () => api("POST", "/instrument", { instrument: i.id }).then(() => { refreshState(true); rerender(); }).catch(fail) })));
  out.push(l);
  if (ins.current === "guitar") {
    out.push(h("h3", {}, "Guitar connection"),
      seg([["audio", "Guitar jack into audio input"], ["midi", "MIDI guitar / pickup"]], ins.guitar_source,
        (v) => api("POST", "/instrument", { guitar_source: v }).then(() => { refreshState(true); rerender(); }).catch(fail)),
      h("p", { class: "muted" }, "A guitar's normal jack needs an audio input (line-in or a USB audio interface). The 5-pin DIN MIDI cable carries notes only, so use MIDI mode with a MIDI guitar pickup, or use it for a MIDI foot controller."));
  }
  out.push(h("h3", {}, "Test without hardware"), h("p", { class: "muted" }, "Click the keys, or switch on Computer keys and play A W S E D F T G Y H U J K."), piano());
  out.push(h("h3", {}, "Sound"));
  const l2 = h("div", { class: "list" });
  snd.sounds.forEach(s => l2.append(item({ title: s.name, sub: s.group, active: s.id === snd.current,
    onclick: () => api("POST", "/instrument", { sound: s.id }).then(() => { toast("Sound: " + s.name); refreshState(); rerender(); }).catch(fail) })));
  out.push(l2);
  return out;
}

async function panelDevices() {
  const [au, mi] = await Promise.all([api("GET", "/devices/audio"), api("GET", "/devices/midi")]);
  const out = [h("h3", {}, "MIDI interface")];
  out.push(h("p", {}, mi.connected ? `Connected: ${mi.port}` : "Not connected. Plug in the 5-pin DIN to USB cable; it is picked up automatically within two seconds."));
  const ml = h("div", { class: "list" });
  mi.ports.forEach(p => ml.append(item({ title: p.name, sub: `Port ${p.index}`, active: p.name === mi.port,
    onclick: () => api("POST", "/devices/midi/connect", { port: p.name }).then(() => { toast("MIDI: " + p.name); refreshState(); rerender(); }).catch(fail) })));
  if (!mi.ports.length) ml.append(h("p", { class: "muted" }, `No MIDI inputs found (backend: ${mi.backend}).`));
  out.push(ml, h("div", { class: "row" },
    h("button", { class: "btn", onclick: rerender }, "Refresh"),
    mi.connected ? h("button", { class: "btn", onclick: () => api("POST", "/devices/midi/disconnect").then(() => { refreshState(); rerender(); }).catch(fail) }, "Disconnect") : null,
    lamp("Auto-connect", mi.auto_connect, (v) => api("POST", "/devices/midi/auto", { enabled: v }).catch(fail), "green")));
  const logBox = h("div", { class: "midilog", "aria-live": "off" }, "Waiting for MIDI…");
  out.push(h("h3", {}, "MIDI monitor"), logBox);
  const pollLog = async () => {
    if (currentPanel !== "devices") return;
    try { const r = await api("GET", "/midi/log"); if (r.log.length) logBox.replaceChildren(...r.log.slice(0, 30).map(x => h("div", {}, `${x.text}  ·  ${x.source}`))); } catch (_) { }
    setTimeout(pollLog, 500);
  };
  setTimeout(pollLog, 200);

  out.push(h("h3", {}, "Audio"));
  const cur = au.current;
  out.push(h("p", { class: "muted" }, `Engine: ${cur.backend}${cur.error ? " — " + cur.error : ""}. Output goes to the computer speakers, or to the headphone/line jack for a hi-fi or guitar amp (3.5 mm to RCA lead).`));
  const sel = (id, label, list, value) => {
    const s = h("select", { id }, h("option", { value: "" }, "System default"));
    list.forEach(d => s.append(h("option", { value: d.index, selected: String(d.index) === String(value) ? "selected" : null }, `${d.name} (${d.hostapi})`)));
    return h("div", { class: "field" }, h("label", { for: id }, label), s);
  };
  const outSel = sel("dev-out", "Output (speakers / line out)", au.outputs, cur.output);
  const inSel = sel("dev-in", "Input (guitar / line in)", au.inputs, cur.input);
  const sr = h("select", { id: "dev-sr" }, ...[44100, 48000, 96000].map(v => h("option", { value: v, selected: v === cur.sample_rate ? "selected" : null }, `${v} Hz`)));
  const bs = h("select", { id: "dev-bs" }, ...[64, 128, 256, 512, 1024].map(v => h("option", { value: v, selected: v === cur.block_size ? "selected" : null }, `${v} samples (${(v / cur.sample_rate * 1000).toFixed(1)} ms)`)));
  const ich = h("select", { id: "dev-ich" }, h("option", { value: 0, selected: cur.input_channel == 0 ? "selected" : null }, "Input 1 / left"), h("option", { value: 1, selected: cur.input_channel == 1 ? "selected" : null }, "Input 2 / right"));
  out.push(outSel, inSel,
    h("div", { class: "field" }, h("label", { for: "dev-ich" }, "Guitar input channel"), ich),
    h("div", { class: "field" }, h("label", { for: "dev-sr" }, "Sample rate"), sr),
    h("div", { class: "field" }, h("label", { for: "dev-bs" }, "Buffer size (lower = less latency, more CPU)"), bs),
    h("button", { class: "btn primary", onclick: async () => {
      const v = (el) => el.value === "" ? null : +el.value;
      try {
        await api("POST", "/devices/audio", { audio_output: v($("select", outSel)), audio_input: v($("select", inSel)),
          sample_rate: +sr.value, block_size: +bs.value, input_channel: +ich.value });
        toast("Audio restarted"); refreshState(true); rerender();
      } catch (e) { fail(e); }
    } }, "Apply and restart audio"));
  return out;
}

async function panelRecordings() {
  const r = await api("GET", "/recordings");
  const out = [h("p", {}, "Use the tape deck under the VU meters to record what you play, effects included, as a 24-bit WAV file tagged with your name, the audio name and the date.")];
  const l = h("div", { class: "list" });
  r.recordings.forEach(f => {
    const audio = h("audio", { controls: true, preload: "none", src: f.url, style: { width: "100%", marginTop: "6px" } });
    l.append(h("div", { class: "item", style: { flexDirection: "column", alignItems: "stretch" } },
      h("div", { class: "row", style: { margin: 0 } },
        h("span", { class: "grow" }, h("span", { class: "t" }, f.title || f.file),
          h("span", { class: "s" }, [f.artist, f.date, clock(f.seconds)].filter(Boolean).join(" · "))),
        h("button", { class: "btn small primary", onclick: () => deckLoad(f.file, true) }, "Play on deck"),
        h("a", { class: "btn small", href: f.url + "?download=1", download: f.file }, "Download"),
        h("button", { class: "btn small danger", onclick: () => {
          if (!confirm(`Delete ${f.file}? This cannot be undone.`)) return;
          api("DELETE", `/recordings/${encodeURIComponent(f.file)}`).then(() => { toast("Recording deleted"); rerender(); }).catch(fail);
        } }, "Delete")),
      audio));
  });
  if (!r.recordings.length) l.append(h("p", { class: "muted" }, "No recordings yet."));
  out.push(l);
  return out;
}

function setLearn(on) {
  S.learnMode = on;
  document.body.classList.toggle("learning", on);
  if (!on) { $$(".learn").forEach(k => k.classList.remove("learn")); api("POST", "/midi/learn", { target: null }).catch(() => { }); }
}

async function panelMidiMap() {
  const r = await api("GET", "/midi/map");
  const out = [h("p", {}, "Link the knobs and sliders on your MIDI controller to the console. Switch on Learn, click a control on screen, then move the control on your device.")];
  out.push(h("div", { class: "row" }, lamp("Learn", S.learnMode, (v) => { setLearn(v); if (v) closePanel(true); toast(v ? "Learn is on: click a knob or fader, then move a control on your MIDI device" : "Learn is off"); }, "")));
  const l = h("div", { class: "list" });
  Object.entries(r.map).forEach(([key, target]) => {
    const [ch, cc] = key.split(":");
    l.append(item({ title: target, sub: `CC ${cc} on channel ${ch}`,
      right: h("button", { class: "btn small danger", onclick: () => api("DELETE", `/midi/map/${encodeURIComponent(key)}`).then(() => { rerender(); refreshState(true); }).catch(fail) }, "Remove") }));
  });
  if (!Object.keys(r.map).length) l.append(h("p", { class: "muted" }, "Nothing mapped yet."));
  out.push(h("h3", {}, "Mappings"), l,
    h("p", { class: "muted" }, "Always on: CC 64 sustain pedal, pitch bend, and Program Change to select presets."));
  return out;
}

async function panelSettings() {
  const [s, p] = await Promise.all([api("GET", "/settings"), loadPresets()]);
  const st = s.settings;
  const startSel = h("select", { id: "set-start" }, h("option", { value: "" }, "None - start with an empty chain"),
    ...p.presets.map(x => h("option", { value: x.id, selected: x.id === st.startup_preset ? "selected" : null }, x.name)));
  return [
    h("div", { class: "field" }, h("label", { for: "set-start" }, "Preset loaded at start-up"), startSel),
    h("button", { class: "btn primary", onclick: () => api("PATCH", "/settings", { startup_preset: startSel.value || null }).then(() => toast("Settings saved")).catch(fail) }, "Save settings"),
    h("h3", {}, "Network"),
    h("p", {}, `Open the console from any device on your network at port ${st.port}. MIDI over UDP (for the ESP32 interface) listens on port ${st.udp_midi_port}.`),
    h("p", { class: "muted" }, "Settings live in data/config.json next to vundu_midi.py."),
  ];
}

async function panelAbout() {
  const sys = await api("GET", "/system");
  return [
    h("p", {}, `${sys.app} ${sys.version} on ${sys.platform} (${sys.machine}), Python ${sys.python}.`),
    h("h3", {}, "Getting started"),
    h("ol", {},
      h("li", {}, "Plug the 5-pin DIN to USB MIDI cable into the computer and your instrument's MIDI OUT."),
      h("li", {}, "Open Instruments and choose Keyboard or Guitar."),
      h("li", {}, "Load a preset, or build your own chain from Plugins."),
      h("li", {}, "Play. Press Record to capture a WAV file.")),
    h("h3", {}, "Controls"),
    h("p", {}, "Drag knobs up or down (hold Shift for fine moves), use the mouse wheel, or the arrow keys. Double-click resets a control. Faders work the same way."),
    h("h3", {}, "Credits"),
    h("p", {}, "Vundu_MIDI is an open source app based on the open source software ",
      h("a", { href: "https://github.com/shakfu/minihost", target: "_blank", rel: "noopener" }, "Minihost"),
      ". All attribution for Minihost belongs to its developers."),
    h("p", { class: "muted" }, sys.minihost.available ? `Minihost ${sys.minihost.version} is installed.` : `Minihost is not installed (${sys.minihost.error || "import failed"}).`),
    h("h3", {}, "API"),
    h("p", {}, "Every control here is also available over HTTP. See ", h("a", { href: "/api/v1", target: "_blank" }, "/api/v1"), " and docs/API.md."),
  ];
}

async function openPluginEditor(slotId) {
  currentPanel = null;
  $("#drawer-title").textContent = "Plugin parameters";
  $("#drawer").classList.add("open"); $("#scrim").hidden = false;
  const body = $("#drawer-body"); body.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try {
    const { slot } = await api("GET", `/chain/${slotId}`);
    const out = [h("h3", {}, slot.name), h("p", { class: "muted" }, `${slot.vendor || ""} ${slot.format || ""} — ${slot.path}`)];
    if (slot.programs?.length) {
      const ps = h("select", { onchange: (e) => api("PATCH", `/chain/${slotId}`, { program: +e.target.value }).then(() => openPluginEditor(slotId)).catch(fail) },
        ...slot.programs.map((n, i) => h("option", { value: i }, n || `Program ${i + 1}`)));
      out.push(h("div", { class: "field" }, h("label", {}, "Factory program"), ps));
    }
    const row = h("div", { class: "knob-row" });
    slot.params.forEach(p => row.append(makeKnob({ ...p, kind: "plugin" }, { color: slot.color,
      onChange: (v) => sendThrottled(`${slotId}.${p.id}`, () => api("PATCH", `/chain/${slotId}`, { params: { [p.id]: v } })) }).el));
    out.push(row);
    body.replaceChildren(...out);
  } catch (e) { body.replaceChildren(h("p", {}, e.message)); }
}

/* ------------------------------------------------------------------ */
/* Computer keyboard as a MIDI keyboard                               */
/* ------------------------------------------------------------------ */
const KEYMAP = { a: 0, w: 1, s: 2, e: 3, d: 4, f: 5, t: 6, g: 7, y: 8, h: 9, u: 10, j: 11, k: 12, o: 13, l: 14 };
let octave = 60;
const held = new Set();
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  if (e.key === "Escape") closePanel();
  if (!S.kbd || e.repeat || e.metaKey || e.ctrlKey) return;
  const k = e.key.toLowerCase();
  if (k === "z") { octave = Math.max(24, octave - 12); toast(`Octave C${octave / 12 - 1}`); return; }
  if (k === "x") { octave = Math.min(96, octave + 12); toast(`Octave C${octave / 12 - 1}`); return; }
  if (k in KEYMAP && !held.has(k)) { held.add(k); noteOn(octave + KEYMAP[k]); e.preventDefault(); }
});
document.addEventListener("keyup", (e) => {
  const k = e.key.toLowerCase();
  if (held.has(k)) { held.delete(k); noteOff(octave + KEYMAP[k]); }
});

/* ------------------------------------------------------------------ */
/* Tape deck                                                          */
/* ------------------------------------------------------------------ */
const clock = (sec) => { sec = Math.max(0, +sec || 0); return `${String(Math.floor(sec / 60)).padStart(2, "0")}:${(sec % 60).toFixed(1).padStart(4, "0")}`; };
const DECK_STATES = { empty: "No take", stopped: "Stopped", playing: "Playing", paused: "Paused", recording: "Recording", rec_paused: "Rec paused" };
let lastDeckFile = null, lastDeckState = null;

function updateDeck(d) {
  if (!d) return;
  const deck = $("#deck");
  deck.dataset.state = d.state;
  $("#deck-state").textContent = DECK_STATES[d.state] || d.state;
  $("#deck-file").textContent = d.file || "No take loaded";
  $("#deck-pos").textContent = clock(d.position);
  $("#deck-len").textContent = clock(d.length);
  const t = d.take || {};
  $("#deck-title").textContent = d.file ? [t.title || "Untitled", t.artist, t.date].filter(Boolean).join("  ·  ")
    : "Press record to start a take";
  const pct = d.length ? (d.position / d.length) * 100 : 0;
  $("#deck-fill").style.width = pct.toFixed(2) + "%";
  $("#deck-bar").setAttribute("aria-valuenow", Math.round(pct));
  const rec = d.state === "recording" || d.state === "rec_paused";
  $("#dk-rec").classList.toggle("lit", rec);
  $("#dk-play").classList.toggle("lit", d.state === "playing");
  $("#dk-pause").classList.toggle("lit", d.state === "paused" || d.state === "rec_paused");
  ["#dk-rew", "#dk-ff"].forEach(id => { $(id).disabled = rec || !d.file; });
  $("#dk-play").disabled = rec || !d.file;
  if (d.file !== lastDeckFile || (lastDeckState && (lastDeckState === "recording" || lastDeckState === "rec_paused") && !rec)) loadTakes();
  lastDeckFile = d.file; lastDeckState = d.state;
}

function renderDeckMeta() {
  const meta = S.state?.deck?.meta; if (!meta) return;
  const set = (id, v) => { const el = $(id); if (document.activeElement !== el) el.value = v || ""; };
  set("#deck-artist", meta.artist);
  set("#deck-title-in", meta.title);
  set("#deck-date", meta.date || new Date().toISOString().slice(0, 10));
}

function deckMeta() {
  return { artist: $("#deck-artist").value.trim(), title: $("#deck-title-in").value.trim(), date: $("#deck-date").value };
}

async function loadTakes() {
  try {
    const r = await api("GET", "/recordings");
    const sel = $("#deck-takes");
    sel.replaceChildren(h("option", { value: "" }, r.recordings.length ? "Load a take…" : "No takes yet"),
      ...r.recordings.map(f => h("option", { value: f.file, selected: f.file === r.loaded ? "selected" : null },
        `${f.title || f.file}${f.artist ? " - " + f.artist : ""} (${clock(f.seconds)})`)));
  } catch (_) { }
}

async function deckLoad(file, play = false) {
  try {
    await api("POST", play ? "/deck/play" : "/deck/load", { file });
    toast(play ? "Playing on the deck" : "Take loaded");
    if (play && currentPanel) closePanel();
  } catch (e) { fail(e); }
}

function holdButton(btn, action) {
  // click = one jump of 5 s; hold = keep winding 2 s every 150 ms
  let timer = null, held = false;
  const stop = () => { clearInterval(timer); timer = null; btn.classList.remove("held"); };
  btn.addEventListener("pointerdown", (e) => {
    if (btn.disabled) return;
    held = false; btn.setPointerCapture(e.pointerId);
    timer = setTimeout(() => { held = true; btn.classList.add("held"); timer = setInterval(() => action(2), 150); }, 350);
  });
  btn.addEventListener("pointerup", () => { const wasHeld = held; stop(); if (!wasHeld && !btn.disabled) action(5); });
  btn.addEventListener("pointercancel", stop);
  btn.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); action(5); } });
}

function wireDeck() {
  const metaChanged = () => sendThrottled("deckmeta", () => api("POST", "/deck/meta", deckMeta()), 400);
  ["#deck-artist", "#deck-title-in", "#deck-date"].forEach(id => $(id).addEventListener("input", metaChanged));
  $("#dk-rec").addEventListener("click", () => {
    const st = $("#deck").dataset.state;
    if (st === "recording") return toast("Already recording - press Pause or Stop");
    const m = deckMeta();
    api("POST", "/deck/record", m).then(r => toast(st === "rec_paused" ? "Recording resumed" : `Recording: ${m.title || "Untitled"}`)).catch(fail);
  });
  $("#dk-pause").addEventListener("click", () => api("POST", "/deck/pause").catch(fail));
  $("#dk-stop").addEventListener("click", () => api("POST", "/deck/stop").then(r => {
    if (r.recording) toast(`Saved ${r.recording.file} (${clock(r.recording.seconds)})`);
  }).catch(fail));
  $("#dk-play").addEventListener("click", () => api("POST", "/deck/play").catch(fail));
  holdButton($("#dk-rew"), (s) => api("POST", "/deck/rewind", { seconds: s }).catch(fail));
  holdButton($("#dk-ff"), (s) => api("POST", "/deck/forward", { seconds: s }).catch(fail));
  $("#deck-takes").addEventListener("change", (e) => e.target.value && deckLoad(e.target.value));
  const bar = $("#deck-bar");
  const seekTo = (frac) => {
    const len = S.meters?.deck?.length || 0;
    if (len && !["recording", "rec_paused"].includes($("#deck").dataset.state)) api("POST", "/deck/seek", { position: frac * len }).catch(fail);
  };
  bar.addEventListener("click", (e) => { const r = bar.getBoundingClientRect(); seekTo((e.clientX - r.left) / r.width); });
  bar.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") api("POST", "/deck/rewind", { seconds: 5 }).catch(fail);
    if (e.key === "ArrowRight") api("POST", "/deck/forward", { seconds: 5 }).catch(fail);
  });
  loadTakes();
}

/* ------------------------------------------------------------------ */
/* Boot                                                               */
/* ------------------------------------------------------------------ */
async function loadSounds() { try { S.sounds = (await api("GET", "/sounds")).sounds; } catch (_) { } }

function wire() {
  $$(".menu button").forEach(b => b.addEventListener("click", () => currentPanel === b.dataset.panel ? closePanel() : openPanel(b.dataset.panel)));
  $("#drawer-close").addEventListener("click", () => closePanel());
  $("#scrim").addEventListener("click", () => closePanel());
  $("#learn-off").addEventListener("click", () => { setLearn(false); toast("Learn is off"); });
  wireDeck();
  $("#btn-prev").addEventListener("click", () => stepPreset(-1));
  $("#btn-next").addEventListener("click", () => stepPreset(1));
  $("#btn-save").addEventListener("click", () => openPanel("presets"));
  $("#btn-panic").addEventListener("click", () => api("POST", "/panic").then(() => toast("All notes off")).catch(fail));
  $("#btn-kbd").addEventListener("click", () => {
    S.kbd = !S.kbd; $("#btn-kbd").setAttribute("aria-pressed", S.kbd);
    toast(S.kbd ? "Computer keys on: A W S E D F T G… (Z/X change octave)" : "Computer keys off");
  });
  $("#clip-led").addEventListener("click", () => api("POST", "/meters/reset_clip").catch(fail));
}

(async function boot() {
  wire();
  await Promise.all([loadSounds(), loadPresets().catch(() => { })]);
  await refreshState(true);
  connectStream();
  requestAnimationFrame(frame);
  try { const s = await api("GET", "/system"); $("#foot-net").textContent = `LAN: http://${s.lan_ip}:${location.port || 80}`; } catch (_) { }
})();
