// Kurier status page — shared helpers: formatters, DOM builders, API key,
// fetch wrapper, formatting. Loaded first after i18n.js; the other scripts
// (now.js, insights.js, app.js) share its top-level names.
// Every value from the API (including the text of received SMS, which is
// untrusted) is rendered with textContent, never as HTML; no inline styles
// (CSP style-src 'self').
"use strict";

const KEY_STORE = "smsgw.apiKey";

// Locale-dependent formatters (i18n.js: LANG, locale(), t()); rebuilt on a language switch.
let nf, rtf, dtf, dayFmt, dayLong;
function setupFormatters() {
  const loc = locale();
  nf = new Intl.NumberFormat(loc);
  rtf = new Intl.RelativeTimeFormat(LANG, { numeric: "auto", style: "short" });
  dtf = new Intl.DateTimeFormat(loc, { dateStyle: "medium", timeStyle: "medium" });
  dayFmt = new Intl.DateTimeFormat(loc, { day: "numeric", month: "short" });
  dayLong = new Intl.DateTimeFormat(loc, { weekday: "long", day: "numeric", month: "long" });
}

// ── DOM helpers ──────────────────────────────────────────────
function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  for (const c of children) node.append(c);
  return node;
}
const $ = (id) => document.getElementById(id);

// ── API key ──────────────────────────────────────────────────
function getKey() {
  try { return localStorage.getItem(KEY_STORE) || ""; } catch { return ""; }
}
function setKey(v) {
  try { v ? localStorage.setItem(KEY_STORE, v) : localStorage.removeItem(KEY_STORE); } catch { /* private mode */ }
}
// People paste the whole .env line, quotes, or stray spaces — accept those.
function cleanKey(raw) {
  let k = (raw || "").trim();
  k = k.replace(/^(export\s+)?SMS_API_KEY\s*=\s*/, "");
  k = k.replace(/^(["'])(.*)\1$/, "$2").trim();
  return k;
}
// crypto.randomUUID needs a secure context; the page is served over plain
// HTTP on the LAN, so build the key from getRandomValues.
function randomKey() {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

class AuthError extends Error {}

async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  const key = getKey();
  if (key) headers["X-API-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, {
    method, headers, cache: "no-store",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401) throw new AuthError("401");
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { const j = await r.json(); if (typeof j.detail === "string") detail = j.detail; } catch { /* not JSON */ }
    const err = new Error(detail);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

// ── Formatting ───────────────────────────────────────────────

function ago(iso) {
  if (!iso) return "";
  const s = Math.round((Date.parse(iso) - Date.now()) / 1000);
  const a = Math.abs(s);
  if (a < 45) return t("ago.now");
  if (a < 3600) return rtf.format(Math.round(s / 60), "minute");
  if (a < 86400) return rtf.format(Math.round(s / 3600), "hour");
  return rtf.format(Math.round(s / 86400), "day");
}
function abs(iso) { return iso ? dtf.format(new Date(iso)) : ""; }
function timeEl(iso) { return el("time", { datetime: iso, title: abs(iso), text: ago(iso) }); }
function minutes(n) { return t("minutes", { n }); }
function attempts(n) { return t("attempts", { n }); }
function duration(sec) {
  if (sec < 60) return `${sec} s`;
  const m = Math.ceil(sec / 60);
  if (m < 60) return `${m} min`;
  if (m < 1440) return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m / 60} h`;
  const h = Math.round(m / 60);
  return h % 24 ? `${Math.floor(h / 24)} d ${h % 24} h` : `${h / 24} d`;
}
function localDay(iso) { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); }

// LTE: RSRP is the meaningful level; fall back to RSSI.
function signalLevel(m) {
  if (typeof m.rsrp === "number") {
    return m.rsrp >= -80 ? 4 : m.rsrp >= -90 ? 3 : m.rsrp >= -100 ? 2 : m.rsrp >= -110 ? 1 : 0;
  }
  if (typeof m.rssi === "number") {
    return m.rssi >= -65 ? 4 : m.rssi >= -75 ? 3 : m.rssi >= -85 ? 2 : m.rssi >= -95 ? 1 : 0;
  }
  return null;
}
function bars(level) {
  const wrap = el("span", { class: "bars", "aria-hidden": "true" });
  for (let i = 1; i <= 4; i++) wrap.append(el("i", { class: `b${i}${i <= level ? " on" : ""}` }));
  return wrap;
}

// Two-step confirm on one button: the first click arms it (label changes),
// the second performs the action; it disarms itself after a few seconds.
// Labels are i18n keys, resolved when shown (the language can change).
function confirmable(button, idleKey, armedKey, action, { cancel } = {}) {
  let timer = null;
  button.textContent = t(idleKey);
  const disarm = () => {
    clearTimeout(timer);
    button.textContent = t(idleKey);
    button.classList.remove("confirm");
    button.dataset.armed = "";
    if (cancel) cancel.hidden = true;
  };
  button.addEventListener("click", async () => {
    if (button.dataset.armed !== "1") {
      button.dataset.armed = "1";
      button.textContent = t(armedKey);
      button.classList.add("confirm");
      if (cancel) cancel.hidden = false;
      timer = setTimeout(disarm, 8000);
      return;
    }
    disarm();
    button.disabled = true;
    try { await action(); } finally { button.disabled = false; }
  });
  if (cancel) cancel.addEventListener("click", disarm);
}
