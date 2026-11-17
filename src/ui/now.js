// Kurier status page — tab "Now": router path, budget, counters, outbox,
// sent/received threads, test SMS, and the refresh loop that feeds them.
"use strict";

const REFRESH_MS = 15000;
const PAGE = 10;
const shown = { sent: PAGE, received: PAGE };
const opened = new Set();  // bubbles the user expanded survive refreshes

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
    el("span", {}, el("i", { class: `dot ${mqtt.enabled === false ? "" : mqtt.connected ? "ok" : "bad"}` }),
      mqtt.enabled === false ? t("conn.mqttNone")
        : mqtt.connected ? t("conn.mqttOn") : t("conn.mqttOff", { broker: mqtt.broker || "broker" })),
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

// ── Watchdogs ────────────────────────────────────────────────
function renderWatchdogs(items) {
  $("watchdogs-section").hidden = !items.length;
  $("watchdogs").replaceChildren(...items.map((w) => {
    const down = w.state === "down";
    return el("li", {},
      el("i", { class: `dot ${down ? "bad" : "ok"}` }),
      el("strong", { text: w.name }),
      el("span", { class: `state${down ? " bad" : ""}`, text: t(down ? "wd.down" : "wd.up") }),
      el("div", { class: "meta" },
        down ? el("span", {}, `${t("wd.downSince")} `, timeEl(w.down_since)) : null,
        down ? el("span", { class: "tag bad",
                            text: w.down_reason === "fail" ? t("wd.failed", { detail: w.detail || "—" }) : t("wd.silent") }) : null,
        down ? el("span", { text: t(w.down_sms_sent ? "wd.smsSent" : "wd.smsHeld") }) : null,
        el("span", {}, `${t("wd.lastPing")} `, timeEl(w.last_ping)),
        el("span", { text: t("wd.timeout", { d: duration(w.timeout_sec) }) })));
  }));
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

// ── Refresh loop (tab "Now") ─────────────────────────────────
let timer = null;

async function refresh() {
  // Each part renders on its own: a router outage makes /api/sms/status
  // fail (502) and /api/health slow — exactly when this page matters most.
  const calls = {
    health: api("/api/health"), root: api("/"), status: api("/api/sms/status"),
    metrics: api("/api/sms/metrics"), budget: api("/api/sms/budget"),
    history: api("/api/sms/history?limit=100"), outbox: api("/api/sms/outbox"),
    inbox: api("/api/sms/inbox?limit=100"), watchdogs: api("/api/watchdogs"),
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
  if (ok("watchdogs")) renderWatchdogs(ok("watchdogs"));
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
