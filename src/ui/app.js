// Kurier status page — entry point: tabs, language switch, event wiring.
// Script order (index.html): i18n.js → core.js → now.js → insights.js → app.js.
"use strict";

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
