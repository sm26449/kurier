// Kurier status page — tabs "Statistics" (summary, chart, tables) and
// "Search" (history search; rows reuse now.js::sentItem).
"use strict";

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
