// Kurier status page. Every value from the API (including the text of
// received SMS, which is untrusted) is rendered with textContent, never as
// HTML; no inline styles (CSP style-src 'self').
"use strict";

const REFRESH_MS = 15000;
const PAGE = 10;
const KEY_STORE = "smsgw.apiKey";
const shown = { sent: PAGE, received: PAGE };
const opened = new Set();  // bubbles the user expanded survive refreshes

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
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
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

// ── Path (hero) ──────────────────────────────────────────────
function renderPath(h, st, m, apiOk) {
  const sig = { rsrp: m.rsrp, rssi: m.rssi ?? st.signal_strength };
  const lvl = signalLevel(sig);
  const signalText = typeof sig.rsrp === "number" ? `RSRP ${sig.rsrp} dBm`
    : typeof sig.rssi === "number" ? `RSSI ${sig.rssi} dBm` : t("signal.unknown");
  const net = [st.operator, st.band || st.connection_type].filter(Boolean).join(" ");
  const temp = typeof st.modem_temperature === "number" ? `${Math.round(st.modem_temperature)} °C` : "";
  const waitMin = Math.round((h.outbox_oldest_age_seconds || 0) / 60);

  const hops = [
    { name: t("hop.gateway"), state: apiOk ? "ok" : "warn",
      detail: apiOk ? t("hop.started", { ago: ago(new Date(Date.now() - (h.uptime_seconds || 0) * 1000).toISOString()) }) : t("hop.noKey") },
    { name: t("hop.router"), state: h.modem_reachable ? "ok" : "bad",
      detail: h.modem_reachable ? [st.modem_model || t("hop.answers"), temp].filter(Boolean).join(", ") : t("hop.down") },
    { name: t("hop.sim"), state: !h.modem_reachable ? "bad" : h.sim_ok ? "ok" : "bad",
      detail: h.sim_ok ? (st.sim_state || "OK") : t("hop.simNotReady") },
    { name: t("hop.network"), state: !h.modem_reachable ? "bad" : lvl === null ? "warn" : lvl >= 2 ? "ok" : lvl === 1 ? "warn" : "bad",
      detail: lvl === null ? (net || "—") : [bars(lvl), net || signalText], title: signalText },
    { name: t("hop.queue"), state: h.outbox_pending ? (waitMin >= 10 ? "bad" : "warn") : "ok",
      detail: h.outbox_pending ? t("hop.queueWaiting", { n: h.outbox_pending, age: minutes(waitMin) }) : t("hop.queueEmpty") },
  ];
  $("path").replaceChildren(...hops.map((x) => el("li", { class: "hop", "data-state": x.state, title: x.title },
    el("div", { class: "node" }),
    el("div", { class: "hop-name", text: x.name }),
    el("div", { class: "hop-detail" }, x.detail),
  )));

  const summary = $("path-summary");
  let text, cls;
  if (!h.modem_reachable) {
    text = t("summary.routerDown");
    cls = "bad";
  } else if (!h.sim_ok) {
    text = t("summary.simNotReady");
    cls = "bad";
  } else if (h.outbox_pending && waitMin >= 10) {
    text = t("summary.stuck", { n: h.outbox_pending, age: minutes(waitMin) });
    cls = "bad";
  } else if (h.outbox_pending) {
    text = t("summary.retrying", { n: h.outbox_pending });
    cls = "warn";
  } else {
    text = t("summary.ok");
    cls = "ok";
  }
  summary.textContent = text;
  summary.className = `path-summary ${cls}`;
}

// ── Budget + connections ─────────────────────────────────────
function renderBudget(b) {
  const meters = [
    [t("budget.normalMinute"), b.normal_minute],
    [t("budget.normalHour"), b.normal_hour],
    [t("budget.criticalHour"), b.critical_hour],
  ];
  $("budget").replaceChildren(...meters.map(([label, w]) => {
    const left = Math.max(0, w.limit - w.used);
    const ratio = w.limit ? w.used / w.limit : 0;
    const cls = left === 0 ? "full" : ratio >= 0.7 ? "high" : "";
    const note = left === 0
      ? t("budget.full", { d: duration(w.frees_in_seconds) })
      : w.used ? t("budget.oldest", { d: duration(w.oldest_leaves_in_seconds) })
      : t("budget.none");
    return el("div", { class: "meter" },
      el("div", { class: "meter-label" }, el("span", { text: label }),
        el("strong", { text: t("budget.free", { left, limit: w.limit }) })),
      el("progress", { max: String(w.limit), value: String(Math.min(w.used, w.limit)), class: cls,
                       "aria-label": t("budget.aria", { label, used: w.used, limit: w.limit }) }),
      el("div", { class: `meter-note${left === 0 ? " full" : ""}`, text: note }));
  }));

  const c = b.connections || {};
  const mqtt = c.mqtt || {}, hook = c.webhook || {};
  $("connections").replaceChildren(
    el("span", {}, el("i", { class: `dot ${mqtt.connected ? "ok" : "bad"}` }),
      mqtt.connected ? t("conn.mqttOn") : t("conn.mqttOff", { broker: mqtt.broker || "broker" })),
    el("span", {}, el("i", { class: `dot ${hook.enabled ? (hook.failures ? "bad" : "ok") : ""}` }),
      hook.enabled
        ? t("conn.hookOn", { ok: nf.format(hook.forwarded), fail: nf.format(hook.failures) })
        : t("conn.hookOff")),
    el("span", { text: t("conn.dedup", { min: Math.round(b.dedup_seconds / 60), n: b.dedup_active }) }),
    el("span", { text: t("conn.history", { store: c.history_store === "MySQL" ? "MySQL" : t("conn.memory") }) }),
  );
}

// ── Counters ─────────────────────────────────────────────────
function renderCounts(m) {
  if (typeof m.uptime_seconds === "number") {
    $("since").textContent = ago(new Date(Date.now() - m.uptime_seconds * 1000).toISOString());
  }
  const items = [
    [t("count.sent"), m.sent],
    [t("count.segments"), m.segments_sent],
    [t("count.queued"), m.queued],
    [t("count.delivered"), m.outbox_delivered],
    [t("count.blocked"), m.blocked_rate_limit, m.blocked_rate_limit > 0],
    [t("count.dup"), m.blocked_duplicate],
    [t("count.failed"), m.failed, m.failed > 0],
    [t("count.received"), m.inbox_received],
  ];
  $("counts").replaceChildren(...items.map(([label, value, alert]) =>
    el("div", {}, el("dt", { text: label }),
      el("dd", { class: alert ? "alert" : "", text: value === undefined ? "—" : nf.format(value) }))));
}

// ── Outbox (with cancel) ─────────────────────────────────────
function renderOutbox(items) {
  $("outbox-section").hidden = !items.length;
  $("outbox").replaceChildren(...items.map((i) => {
    const cancel = el("button", { type: "button", class: "link cancel" });
    confirmable(cancel, "outbox.cancel", "outbox.confirm", async () => {
      try {
        await api(`/api/sms/outbox/${i.id}`, { method: "DELETE" });
        refresh();
      } catch (e) {
        cancel.textContent = t("outbox.cancelFailed", { msg: e.message });
      }
    });
    return el("li", {},
      el("strong", { text: i.number }), " — ", i.message.slice(0, 80), cancel,
      el("div", { class: "meta" },
        el("span", {}, `${t("outbox.queuedAt")} `, timeEl(i.created_at)),
        el("span", { text: attempts(i.attempts) }),
        i.last_error ? el("span", { text: t("outbox.lastError", { e: i.last_error }) }) : null));
  }));
}

// ── Threads ──────────────────────────────────────────────────
// Consecutive identical entries (same text, same outcome) collapse into one
// bubble with a count and a time range — a storm reads as one event.
function group(items, keyOf) {
  const out = [];
  for (const it of items) {
    const last = out[out.length - 1];
    if (last && keyOf(last.first) === keyOf(it)) { last.count += 1; last.oldest = it; }
    else out.push({ first: it, oldest: it, count: 1 });
  }
  return out;
}

function bubble(text, id) {
  const inner = el("div", { class: "text", text });
  const b = el("div", { class: "bubble" }, inner);
  if (opened.has(id)) b.classList.add("open");
  b.addEventListener("click", () => {
    b.classList.toggle("open");
    b.classList.contains("open") ? opened.add(id) : opened.delete(id);
  });
  requestAnimationFrame(() => {
    if (!b.classList.contains("open") && inner.scrollHeight <= inner.clientHeight + 1) b.classList.add("short");
  });
  return b;
}

function when(g, key) {
  if (g.count === 1) return timeEl(g.first[key]);
  return el("span", {}, timeEl(g.oldest[key]), " – ", timeEl(g.first[key]));
}

function paginate(listId, groups, render) {
  $(listId).replaceChildren(...groups.slice(0, shown[listId]).map(render));
  $(`${listId}-more`).hidden = groups.length <= shown[listId];
}

function sentStatus(e) {
  const d = e.detail || "";
  if (e.success) return { label: t("st.sent"), cls: "" };
  if (e.queued || d.startsWith("queued")) {
    return { label: e.outbox_id ? t("st.queuedN", { id: e.outbox_id }) : t("st.queued"), cls: "pending", tag: "warn" };
  }
  if (d.startsWith("Rate limit")) return { label: t("st.blocked"), cls: "problem", tag: "bad" };
  if (d.startsWith("Duplicate")) return { label: t("st.dup"), cls: "dim" };
  if (d.startsWith("expired")) return { label: t("st.expired"), cls: "problem", tag: "bad" };
  return { label: t("st.failed"), cls: "problem", tag: "bad" };
}

function sentItem(g, absolute = false) {
  const e = g.first;
  const s = sentStatus(e);
  return el("li", { class: `msg ${s.cls}` },
    bubble(e.message, `s:${e.timestamp}`),
    el("div", { class: "meta" },
      g.count > 1 ? el("span", { class: "count", text: `×${g.count}` }) : null,
      e.priority === "critical" ? el("span", { class: "tag crit", text: t("tag.critical") }) : null,
      el("span", { class: s.tag ? `tag ${s.tag}` : "", text: s.label, title: e.detail || "" }),
      e.source ? el("span", { text: e.type ? `${e.source} / ${e.type}` : e.source }) : null,
      e.segments ? el("span", { text: t("segments.short", { n: e.segments }) }) : null,
      el("span", { text: e.number }),
      absolute ? el("time", { datetime: e.timestamp, text: abs(e.timestamp) }) : when(g, "timestamp")));
}

function renderSent(items) {
  if (!items.length) {
    $("sent").replaceChildren(el("li", { class: "empty", text: t("empty.sent") }));
    $("sent-more").hidden = true;
    return;
  }
  paginate("sent", group(items, (e) => `${e.message}\u0000${sentStatus(e).label}\u0000${e.number}`),
           (g) => sentItem(g));  // not map(sentItem): map's index would be read as `absolute`
}

function renderReceived(items) {
  if (!items.length) {
    $("received").replaceChildren(el("li", { class: "empty", text: t("empty.received") }));
    $("received-more").hidden = true;
    return;
  }
  paginate("received", group(items, (m) => `${m.message}\u0000${m.sender}\u0000${m.stale}`), (g) => {
    const m = g.first;
    const delivered = [m.published && "MQTT", m.forwarded && "Klopot"].filter(Boolean).join(" + ");
    return el("li", { class: `msg${m.stale ? " dim" : ""}` },
      bubble(m.message, `r:${m.id}`),
      el("div", { class: "meta" },
        g.count > 1 ? el("span", { class: "count", text: `×${g.count}` }) : null,
        el("span", { text: m.sender }),
        m.authorized ? null : el("span", { class: "tag", text: t("recv.unknown") }),
        m.stale ? el("span", { class: "tag warn", text: t("recv.stale") })
          : el("span", { text: delivered ? t("recv.delivered", { to: delivered }) : t("recv.notYet") }),
        m.forward_error ? el("span", { class: "tag bad", text: `webhook: ${m.forward_error}` }) : null,
        when(g, "date")));
  });
}

// ── Test SMS ─────────────────────────────────────────────────
function setupTestSms() {
  const out = $("test-result");
  confirmable($("test-btn"), "test.button", "test.armed", async () => {
    out.textContent = t("test.sending");
    try {
      const time = new Date().toLocaleTimeString(locale());
      const res = await api("/api/sms/send", { method: "POST", body: {
        message: t("test.message", { time }),
        source: "ui", type: "ui_test", idempotency_key: `ui-${randomKey()}`,
      } });
      const r = res[0] || {};
      out.textContent = r.queued ? t("test.queued")
        : r.success ? t("test.sent", { n: r.segments })
        : t("test.notSent", { why: r.detail || t("test.unknownReason") });
      refresh();
    } catch (e) {
      out.textContent = e.status === 429 ? t("test.rate", { msg: e.message })
        : e.status === 409 ? t("test.dup")
        : t("test.notSent", { why: e.message });
    }
  }, { cancel: $("test-cancel") });
}

// ── Statistics ───────────────────────────────────────────────
let statsDays = 30;
let statsData = null;
let activeDay = -1;

function niceMax(v) {
  if (v <= 4) return 4;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return v;
}

function readout(i) {
  const d = statsData.per_day[i];
  if (!d) { $("chart-readout").textContent = ""; return; }
  const parts = [t("rd.sent", { n: nf.format(d.sent) })];
  if (d.segments) parts.push(t("rd.segments", { n: nf.format(d.segments) }));
  if (d.blocked) parts.push(t("rd.blocked", { n: nf.format(d.blocked) }));
  if (d.failed) parts.push(t("rd.failed", { n: nf.format(d.failed) }));
  if (d.duplicate) parts.push(t("rd.dup", { n: nf.format(d.duplicate) }));
  if (d.queued) parts.push(t("rd.queued", { n: nf.format(d.queued) }));
  $("chart-readout").textContent = `${dayLong.format(localDay(d.date))}: ${parts.join(", ")}.`;
}

function setActive(i) {
  const cols = $("chart").querySelectorAll(".col");
  if (!cols.length) return;
  activeDay = Math.max(0, Math.min(cols.length - 1, i));
  cols.forEach((c, j) => c.classList.toggle("active", j === activeDay));
  readout(activeDay);
}

function renderChart() {
  const days = statsData.per_day;
  const box = $("chart");
  const W = Math.max(300, box.clientWidth || 600), H = 240;
  const m = { l: 34, r: 4, t: 8, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = niceMax(Math.max(1, ...days.map((d) => d.sent + d.blocked + d.failed)));
  const y = (v) => m.t + ih - (v / max) * ih;
  const band = iw / days.length;
  const bw = Math.max(2, Math.min(28, band * 0.68));

  const grid = svg("g", { class: "grid" });
  const axis = svg("g", { class: "axis" });
  for (const v of [0, max / 2, max]) {
    grid.append(svg("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    const tick = svg("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" });
    tick.textContent = nf.format(v);
    axis.append(tick);
  }
  const step = days.length <= 7 ? 1 : days.length <= 31 ? 7 : 14;
  const cols = svg("g", {});
  days.forEach((d, i) => {
    const cx = m.l + band * i + band / 2;
    const g = svg("g", { class: "col" });
    g.append(svg("rect", { class: "hit", x: m.l + band * i, y: m.t, width: band, height: ih }));
    const bad = d.blocked + d.failed;
    let top = y(0);
    const rx = Math.min(2, bw / 4);
    if (d.sent) {
      g.append(svg("rect", { class: "bar-sent", x: cx - bw / 2, y: y(d.sent), width: bw,
                             height: y(0) - y(d.sent), rx }));
      top = y(d.sent) - 2;  // 2px surface gap between stacked segments
    }
    if (bad) {
      const h = Math.max(1, (bad / max) * ih);
      g.append(svg("rect", { class: "bar-bad", x: cx - bw / 2, y: top - h, width: bw, height: h, rx }));
    }
    g.addEventListener("mouseenter", () => setActive(i));
    cols.append(g);
    const last = i === days.length - 1;
    // Skip a periodic label that would collide with the last one.
    if (last || (i % step === 0 && days.length - 1 - i >= Math.ceil(step / 2))) {
      const label = svg("text", { x: cx, y: H - 6, "text-anchor": last ? "end" : "middle" });
      label.textContent = dayFmt.format(localDay(d.date));
      axis.append(label);
    }
  });
  const chart = svg("svg", {
    viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0",
    "aria-label": t("chart.aria", { n: days.length }),
  }, grid, cols, axis);
  chart.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft") { setActive(activeDay - 1); ev.preventDefault(); }
    if (ev.key === "ArrowRight") { setActive(activeDay + 1); ev.preventDefault(); }
  });
  box.replaceChildren(chart);
  setActive(activeDay >= 0 && activeDay < days.length ? activeDay : days.length - 1);
}

function renderStats() {
  const s = statsData, tot = s.totals;
  const parts = [t("ss.sent", { n: nf.format(tot.sent) })];
  if (tot.segments) parts.push(t("ss.segments", { n: nf.format(tot.segments) }));
  parts.push(t("ss.blocked", { n: nf.format(tot.blocked) }), t("ss.failed", { n: nf.format(tot.failed) }),
             t("ss.dup", { n: nf.format(tot.duplicate) }));
  let text = t("ss.lead", { days: s.days, parts: parts.join(", ") });
  if (s.unknown_segments) text += ` ${t("ss.unknown", { n: nf.format(s.unknown_segments) })}`;
  if (s.partial) text += ` ${t("ss.partial")}`;
  $("stats-summary").textContent = text;
  renderChart();

  const head = (cols) => el("thead", {}, el("tr", {}, ...cols.map((h) => el("th", { scope: "col", text: h }))));
  $("days-table").replaceChildren(
    head(["th.day", "th.sent", "th.segments", "th.blocked", "th.failed", "th.dup", "th.queued"].map((k) => t(k))),
    el("tbody", {}, ...s.per_day.slice().reverse().map((d) => el("tr", {},
      el("td", { text: dayFmt.format(localDay(d.date)) }),
      ...[d.sent, d.segments, d.blocked, d.failed, d.duplicate, d.queued].map((v) => el("td", { text: nf.format(v) }))))));

  const types = s.types.filter((x) => x.sent + x.blocked + x.failed > 0).slice(0, 12);
  $("types-table").replaceChildren(
    head(["th.type", "th.sent", "th.segments", "th.blocked", "th.failed"].map((k) => t(k))),
    el("tbody", {}, ...(types.length ? types.map((x) => el("tr", {},
      el("td", { text: x.type || t("types.none") }),
      ...[x.sent, x.segments, x.blocked, x.failed].map((v) => el("td", { text: nf.format(v) }))))
      : [el("tr", {}, el("td", { colspan: "5", text: t("types.empty") }))])));

  const select = $("s-type");
  const current = select.value;
  select.replaceChildren(el("option", { value: "", text: t("search.any") }),
    ...s.types.filter((x) => x.type).map((x) => el("option", { value: x.type, text: x.type })));
  select.value = current;
}

async function loadStats() {
  $("stats-summary").textContent = t("loading");
  try {
    statsData = await api(`/api/sms/stats?days=${statsDays}`);
    renderStats();
  } catch (e) {
    $("stats-summary").textContent = e instanceof AuthError
      ? t("stats.needKey")
      : t("stats.failed", { msg: e.message });
  }
}

// ── Search ───────────────────────────────────────────────────
let searchOffset = 0;
const SEARCH_PAGE = 30;

function searchParams() {
  const p = new URLSearchParams();
  const q = $("s-q").value.trim();
  if (q) p.set("q", q);
  if ($("s-status").value) p.set("status", $("s-status").value);
  if ($("s-type").value) p.set("type", $("s-type").value);
  if ($("s-critical").checked) p.set("priority", "critical");
  if ($("s-since").value) p.set("since", localDay($("s-since").value).toISOString());
  if ($("s-until").value) {
    const u = localDay($("s-until").value);
    u.setDate(u.getDate() + 1);  // inclusive end day
    p.set("until", u.toISOString());
  }
  p.set("limit", String(SEARCH_PAGE + 1));
  p.set("offset", String(searchOffset));
  return p;
}

async function runSearch(append) {
  if (!append) searchOffset = 0;
  $("search-note").textContent = t("search.searching");
  try {
    const rows = await api(`/api/sms/history/search?${searchParams()}`);
    const more = rows.length > SEARCH_PAGE;
    const page = rows.slice(0, SEARCH_PAGE).map((e) => sentItem({ first: e, oldest: e, count: 1 }, true));
    if (append) $("results").append(...page);
    else $("results").replaceChildren(...page);
    searchOffset += Math.min(rows.length, SEARCH_PAGE);
    $("results-more").hidden = !more;
    $("search-note").textContent = searchOffset === 0
      ? t("search.none")
      : t("search.count", { n: nf.format(searchOffset), more });
  } catch (e) {
    $("search-note").textContent = e instanceof AuthError
      ? t("search.needKey")
      : t("search.failed", { msg: e.message });
  }
}

// ── Tabs ─────────────────────────────────────────────────────
const TABS = { now: "panel-now", stats: "panel-stats", search: "panel-search" };

function showTab(name, focus) {
  if (!TABS[name]) name = "now";
  for (const [n, panel] of Object.entries(TABS)) {
    const tab = $(`tab-${n}`);
    const on = n === name;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(panel).hidden = !on;
    if (on && focus) tab.focus();
  }
  try { history.replaceState(null, "", name === "now" ? location.pathname : `#${name}`); } catch { /* ignore */ }
  if (name === "now") refresh();
  if ((name === "stats" || name === "search") && !statsData) loadStats();
  if (name === "stats" && statsData) renderChart();
}

// ── Refresh loop (tab "Now") ─────────────────────────────────
let timer = null;

async function refresh() {
  // Each part renders on its own: a router outage makes /api/sms/status
  // fail (502) and /api/health slow — exactly when this page matters most.
  const calls = {
    health: api("/api/health"), root: api("/"), status: api("/api/sms/status"),
    metrics: api("/api/sms/metrics"), budget: api("/api/sms/budget"),
    history: api("/api/sms/history?limit=100"), outbox: api("/api/sms/outbox"),
    inbox: api("/api/sms/inbox?limit=100"),
  };
  const names = Object.keys(calls);
  const settled = await Promise.allSettled(Object.values(calls));
  const r = Object.fromEntries(names.map((n, i) => [n, settled[i]]));
  const ok = (n) => (r[n].status === "fulfilled" ? r[n].value : null);
  const authFailed = settled.some((x) => x.status === "rejected" && x.reason instanceof AuthError);

  if (!ok("health")) {
    $("path-summary").textContent = t("gw.down");
    $("path-summary").className = "path-summary bad";
    $("updated").textContent = "";
    return;
  }
  const metrics = ok("metrics") || {};
  $("version").textContent = ok("root")?.version ? `v${ok("root").version}` : "";
  renderPath(ok("health"), ok("status") || {}, metrics, !authFailed);
  if (ok("budget")) renderBudget(ok("budget"));
  if (ok("metrics")) renderCounts(metrics);
  if (ok("outbox")) renderOutbox(ok("outbox"));
  if (ok("history")) renderSent(ok("history"));
  if (ok("inbox")) renderReceived(ok("inbox").messages || []);

  if (authFailed) {
    $("key-form").hidden = false;
    $("key-error").hidden = !getKey();
    const k = getKey();
    $("key-error").textContent = k.startsWith("alertd_") ? t("key.klopot") : t("key.wrong", { n: k.length });
    if (document.activeElement !== $("key-input")) $("key-input").focus();
    $("updated").textContent = "";
    return;
  }
  $("key-form").hidden = true;
  const failed = names.filter((n) => r[n].status === "rejected" && n !== "status");
  $("updated").textContent = failed.length
    ? t("updated.partial", { what: failed.join(", ") })
    : t("updated.at", { time: new Date().toLocaleTimeString(locale()) });
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden && !$("panel-now").hidden) refresh(); }, REFRESH_MS);
}

// ── Language ─────────────────────────────────────────────────
let lastSearchRan = false;

function switchLanguage() {
  setLang(LANG === "ro" ? "en" : "ro");
  setupFormatters();
  applyStatic();
  refresh();
  if (statsData) renderStats();
  if (lastSearchRan) runSearch(false);
}

document.addEventListener("DOMContentLoaded", () => {
  setupFormatters();
  applyStatic();
  $("lang-toggle").addEventListener("click", switchLanguage);
  $("key-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    setKey(cleanKey($("key-input").value));
    $("key-input").value = "";
    statsData = null;
    refresh();
    if (!$("panel-stats").hidden || !$("panel-search").hidden) loadStats();
  });
  $("key-clear").addEventListener("click", () => { setKey(""); refresh(); });
  $("key-toggle").addEventListener("click", () => {
    const f = $("key-form");
    f.hidden = !f.hidden;
    if (!f.hidden) $("key-input").focus();
  });
  for (const id of ["sent", "received"]) {
    $(`${id}-more`).addEventListener("click", () => { shown[id] += PAGE; refresh(); });
  }

  const order = Object.keys(TABS);
  for (const name of order) {
    const tab = $(`tab-${name}`);
    tab.addEventListener("click", () => showTab(name));
    tab.addEventListener("keydown", (ev) => {
      const i = order.indexOf(name);
      if (ev.key === "ArrowRight") showTab(order[(i + 1) % order.length], true);
      if (ev.key === "ArrowLeft") showTab(order[(i + order.length - 1) % order.length], true);
    });
  }
  for (const b of document.querySelectorAll(".period button")) {
    b.addEventListener("click", () => {
      statsDays = Number(b.dataset.days);
      for (const o of document.querySelectorAll(".period button")) o.setAttribute("aria-pressed", String(o === b));
      activeDay = -1;
      loadStats();
    });
  }
  $("search-form").addEventListener("submit", (ev) => { ev.preventDefault(); lastSearchRan = true; runSearch(false); });
  $("results-more").addEventListener("click", () => runSearch(true));
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (statsData && !$("panel-stats").hidden) renderChart(); }, 150);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("panel-now").hidden) refresh();
  });

  setupTestSms();
  showTab(location.hash.slice(1));
  schedule();
});
