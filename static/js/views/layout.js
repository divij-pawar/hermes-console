/**
 * views/layout.js — Sidebar resizers, collapsible panels, info pills, clock.
 *
 * No imports from other views — this module only touches DOM geometry.
 */

import { formatDisplayTime, escHtml } from "../util/format.js";
import * as api from "../api/client.js";
import { setUiDragging } from "../util/pollScheduler.js";

const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "local";

// ── Single resize dispatcher ───────────────────────────────────────────────────

const _resizeCallbacks = new Map();
let _resizeScheduled = false;

export function registerResizeCallback(key, fn) {
  _resizeCallbacks.set(key, fn);
}

export function initResizeDispatcher() {
  window.addEventListener("resize", () => {
    if (_resizeScheduled) return;
    _resizeScheduled = true;
    requestAnimationFrame(() => {
      _resizeScheduled = false;
      _resizeCallbacks.forEach(fn => { try { fn(); } catch (_) {} });
    });
  }, { passive: true });
}

// ── Sidebar resizers ──────────────────────────────────────────────────────────

const sidebarResizers = {};

export function setupSidebarResizers(sidebarId, panelIds, storageKey, defaultRatios) {
  const sidebar = document.getElementById(sidebarId);
  if (!sidebar) return;

  const panels = panelIds.map(id => document.getElementById(id)).filter(Boolean);
  if (panels.length < 2) return;

  sidebar.querySelectorAll(".panel-resizer").forEach(r => r.remove());

  const RESIZER_H    = 5;
  const MIN_EXPANDED = 80;
  const MIN_COLLAPSED = 28;

  function isCollapsed(panel) { return panel.classList.contains("panel-collapsed"); }

  function readCollapsedHeight(panel) {
    const title = panel.querySelector(".panel-title");
    if (!title) return MIN_COLLAPSED;
    const style = getComputedStyle(panel);
    const pad   = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    return Math.max(MIN_COLLAPSED, title.offsetHeight + pad);
  }

  function availableHeight() {
    const container = panels[0]?.parentElement || sidebar;
    return container.clientHeight - (panels.length - 1) * RESIZER_H;
  }

  function applyHeights(heights) {
    panels.forEach((panel, i) => { panel.style.flex = `0 0 ${heights[i]}px`; });
  }

  function loadHeights() {
    try { const s = localStorage.getItem(storageKey); if (s) return JSON.parse(s); } catch (_) {}
    return null;
  }

  function saveHeights(heights) { localStorage.setItem(storageKey, JSON.stringify(heights)); }

  function initHeights() {
    const total = availableHeight();
    let heights = loadHeights();
    if (heights && heights.length === panels.length) {
      const sum = heights.reduce((a, b) => a + b, 0);
      if (sum > 0 && Math.abs(sum - total) > 2) heights = heights.map(h => Math.round(h * total / sum));
    } else {
      heights = defaultRatios.map(r => Math.max(MIN_EXPANDED, Math.round(total * r)));
      const sum = heights.reduce((a, b) => a + b, 0);
      heights[heights.length - 1] += total - sum;
    }
    panels.forEach((panel, i) => { if (isCollapsed(panel)) heights[i] = readCollapsedHeight(panel); });
    applyHeights(heights);
    return heights;
  }

  let heights = initHeights();

  for (let i = 0; i < panels.length - 1; i++) {
    const resizer = document.createElement("div");
    resizer.className = "panel-resizer";
    panels[i].after(resizer);

    resizer.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const idx           = i;
      const startY        = e.clientY;
      const collapsedTop    = isCollapsed(panels[idx])     ? readCollapsedHeight(panels[idx])     : null;
      const collapsedBottom = isCollapsed(panels[idx + 1]) ? readCollapsedHeight(panels[idx + 1]) : null;
      if (collapsedTop !== null && collapsedBottom !== null) return;

      const startTop    = collapsedTop    ?? heights[idx];
      const startBottom = collapsedBottom ?? heights[idx + 1];
      const minTop      = collapsedTop    ?? MIN_EXPANDED;
      const minBottom   = collapsedBottom ?? MIN_EXPANDED;

      resizer.classList.add("dragging");
      setUiDragging(true);
      let rafPending = false;
      let lastY = startY;

      function onMove(ev) {
        lastY = ev.clientY;
        if (rafPending) return;
        rafPending = true;
        requestAnimationFrame(() => {
          rafPending = false;
          const dy = lastY - startY;
          let newTop    = startTop    + dy;
          let newBottom = startBottom - dy;
          if (newTop    < minTop)    { newBottom -= minTop    - newTop;    newTop    = minTop; }
          if (newBottom < minBottom) { newTop    -= minBottom - newBottom; newBottom = minBottom; }
          if (collapsedTop    === null) heights[idx]     = newTop;
          if (collapsedBottom === null) heights[idx + 1] = newBottom;
          applyHeights(heights);
        });
      }

      function onUp() {
        resizer.classList.remove("dragging");
        setUiDragging(false);
        saveHeights(heights);
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup",   onUp);
      }

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup",   onUp);
    });
  }

  sidebarResizers[sidebarId] = {
    refresh: () => applyHeights(heights),
    relayout: () => { heights = initHeights(); },
  };

  registerResizeCallback(`sidebar:${sidebarId}`, () => {
    sidebarResizers[sidebarId]?.relayout?.();
  });
}

export function refreshSidebarLayouts() {
  Object.values(sidebarResizers).forEach(ctrl => ctrl.refresh?.());
}

export function relayoutSidebarLayouts() {
  Object.values(sidebarResizers).forEach(ctrl => ctrl.relayout?.());
}

// ── Center column resizer ─────────────────────────────────────────────────────

export function setupCenterResizers() {
  const center = document.getElementById("center");
  if (!center) return;

  const panels = ["kanban", "cron-panel", "feed"].map(id => document.getElementById(id)).filter(Boolean);
  if (panels.length < 2) return;

  center.querySelectorAll(".center-resizer").forEach(r => r.remove());

  const RESIZER_H  = 5;
  const MIN_H      = 80;
  const STORAGE_KEY = "hermes-monitor:center-heights";

  function availableHeight() {
    const healthH = document.getElementById("health-panel")?.offsetHeight || 0;
    return center.clientHeight - healthH - (panels.length - 1) * RESIZER_H;
  }

  function loadHeights() {
    try { const s = localStorage.getItem(STORAGE_KEY); if (s) return JSON.parse(s); } catch (_) {}
    return null;
  }

  function saveHeights(h)   { localStorage.setItem(STORAGE_KEY, JSON.stringify(h)); }
  function applyHeights(h)  { panels.forEach((p, i) => { p.style.flex = `0 0 ${h[i]}px`; }); }

  function initHeights() {
    const total = availableHeight();
    let heights = loadHeights();
    if (heights && heights.length === panels.length) {
      const sum = heights.reduce((a, b) => a + b, 0);
      if (sum > 0 && Math.abs(sum - total) > 2) heights = heights.map(h => Math.round(h * total / sum));
    } else {
      const defaultRatios = [0.35, 0.18, 0.47];
      heights = defaultRatios.map(r => Math.max(MIN_H, Math.round(total * r)));
      const sum = heights.reduce((a, b) => a + b, 0);
      heights[heights.length - 1] += total - sum;
    }
    applyHeights(heights);
    return heights;
  }

  let heights = initHeights();

  for (let i = 0; i < panels.length - 1; i++) {
    const resizer = document.createElement("div");
    resizer.className = "center-resizer";
    panels[i].after(resizer);

    resizer.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const idx      = i;
      const startY   = e.clientY;
      const startTop = heights[idx];
      const startBot = heights[idx + 1];

      resizer.classList.add("dragging");
      setUiDragging(true);
      let rafPending = false;
      let lastY = startY;

      function onMove(ev) {
        lastY = ev.clientY;
        if (rafPending) return;
        rafPending = true;
        requestAnimationFrame(() => {
          rafPending = false;
          const dy  = lastY - startY;
          let newTop = startTop + dy;
          let newBot = startBot - dy;
          if (newTop < MIN_H) { newBot -= MIN_H - newTop; newTop = MIN_H; }
          if (newBot < MIN_H) { newTop -= MIN_H - newBot; newBot = MIN_H; }
          heights[idx]     = newTop;
          heights[idx + 1] = newBot;
          applyHeights(heights);
        });
      }

      function onUp() {
        resizer.classList.remove("dragging");
        setUiDragging(false);
        saveHeights(heights);
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup",   onUp);
      }

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup",   onUp);
    });
  }

  registerResizeCallback("center", () => { heights = initHeights(); });
}

// ── Collapsible panels ────────────────────────────────────────────────────────

export function setupCollapsiblePanels() {
  document.querySelectorAll(".panel-collapse-btn[data-collapse-target]").forEach(btn => {
    const targetId = btn.dataset.collapseTarget;
    const panel    = document.getElementById(targetId);
    if (!panel) return;
    const key = `hermes-monitor:${targetId}:collapsed`;
    const apply = (collapsed) => {
      panel.classList.toggle("panel-collapsed", collapsed);
      btn.textContent = collapsed ? "⌄" : "⌃";
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    };
    apply(localStorage.getItem(key) === "1");
    btn.addEventListener("click", () => {
      const collapsed = !panel.classList.contains("panel-collapsed");
      localStorage.setItem(key, collapsed ? "1" : "0");
      apply(collapsed);
      relayoutSidebarLayouts();
    });
  });
}

// ── Whole-sidebar collapse ────────────────────────────────────────────────────

export function toggleSidebar(side) {
  const cls       = side === "left" ? "left-collapsed" : "right-collapsed";
  const collapsed = document.body.classList.toggle(cls);
  localStorage.setItem(`hermes-monitor:${side}-sidebar-collapsed`, collapsed ? "1" : "0");

  const btn = document.getElementById(`${side}-collapse-btn`);
  if (btn) {
    btn.title = collapsed ? `Expand ${side} sidebar` : `Collapse ${side} sidebar`;
    const icon = btn.querySelector(".sidebar-toggle-icon");
    if (icon) icon.style.transform = collapsed ? "scaleX(-1)" : "";
  }
  relayoutSidebarLayouts();
}

export function restoreSidebarCollapse() {
  ["left", "right"].forEach(side => {
    const val = localStorage.getItem(`hermes-monitor:${side}-sidebar-collapsed`);
    if (val === "1") {
      document.body.classList.add(side === "left" ? "left-collapsed" : "right-collapsed");
      const btn = document.getElementById(`${side}-collapse-btn`);
      if (btn) {
        btn.title = `Expand ${side} sidebar`;
        const icon = btn.querySelector(".sidebar-toggle-icon");
        if (icon) icon.style.transform = "scaleX(-1)";
      }
    }
  });
  relayoutSidebarLayouts();
}

// ── Navbar clock ──────────────────────────────────────────────────────────────

export function updateNavbarClock() {
  const el = document.getElementById("navbar-time");
  if (!el) return;
  el.textContent = formatDisplayTime(Date.now(), { seconds: true, tz: true });
  el.title = LOCAL_TZ;
}

// ── Connection badge ──────────────────────────────────────────────────────────

export function setMonitorConnectionState(state) {
  const badge = document.getElementById("navbar-connection");
  const label = document.getElementById("navbar-connection-label");
  if (!badge || !label) return;
  badge.classList.remove("state-online", "state-offline", "state-unknown");
  badge.classList.add(`state-${state}`);
  label.textContent = state === "online" ? "online" : state === "offline" ? "offline" : "?";
}

export function showConnStatus(msg, cls) {
  const el = document.getElementById("conn-status");
  if (el) { el.textContent = msg; el.className = `visible ${cls}`; }
}

export function hideConnStatus() {
  const el = document.getElementById("conn-status");
  if (el) el.className = "";
}

// ── Info pills (hermes-dir, gateway) ─────────────────────────────────────────

export async function loadInfo() {
  try {
    const info  = await api.fetchInfo();
    const dirEl = document.getElementById("hermes-dir-pill");
    if (dirEl) {
      const dir   = info.hermes_dir || "";
      const short = dir.length > 36 ? "…" + dir.slice(-33) : dir;
      dirEl.textContent = short;
      dirEl.title = "Click to copy: " + dir;
      dirEl.onclick = () => {
        navigator.clipboard?.writeText(dir);
        dirEl.textContent = "✓ copied";
        setTimeout(() => { dirEl.textContent = short; }, 1200);
      };
    }
    const gwEl = document.getElementById("gateway-pill");
    if (gwEl) {
      gwEl.textContent = info.docker_container
        ? `${info.orchestrator}@${info.docker_container}`
        : info.orchestrator || "";
    }
  } catch (_) {}
}

// ── Log popover ───────────────────────────────────────────────────────────────

function _formatJsonish(value) {
  if (value == null || value === "") return "";
  if (typeof value === "string") {
    try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
  }
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

export function openLogViewer(ev, rowEl) {
  const existing = document.getElementById("log-popover");
  if (existing) { existing.remove(); return; }

  const metaFor_label = ev.agent || "?";
  const kindLabel     = (ev.kind || "").replace(/_/g, " ");
  const text          = ev.full || "";
  const isMarkdown    = ["response", "delegation", "subagent_result"].includes(ev.kind);

  const pop = document.createElement("div");
  pop.id = "log-popover";
  pop.className = "log-popover";

  const hdr = document.createElement("div");
  hdr.className = "log-popover-header";
  hdr.innerHTML = `<span class="agent-badge badge-${escHtml(ev.agent)}">${escHtml(metaFor_label)}</span><span class="log-popover-kind">${escHtml(kindLabel)}</span><span class="log-popover-ts">${escHtml(formatDisplayTime(ev.ts || Date.now(), { tz: true }))}</span><button class="log-popover-close" onclick="document.getElementById('log-popover').remove()">×</button>`;

  const body = document.createElement("div");
  body.className = "log-popover-body";

  if (ev.result_full) {
    const callSec = document.createElement("div");
    callSec.className = "log-popover-section";
    callSec.innerHTML = `<div class="log-popover-section-title">Tool Call</div>`;
    const callPre = document.createElement("pre");
    callPre.className = "log-popover-pre";
    callPre.textContent = _formatJsonish(text);
    callSec.appendChild(callPre);
    body.appendChild(callSec);

    const resSec = document.createElement("div");
    resSec.className = "log-popover-section";
    resSec.innerHTML = `<div class="log-popover-section-title">${ev.result_kind === "tool_error" ? "Tool Error" : "Tool Result"}</div>`;
    const resPre = document.createElement("pre");
    resPre.className = "log-popover-pre";
    resPre.textContent = _formatJsonish(ev.result_full);
    resSec.appendChild(resPre);
    body.appendChild(resSec);
  } else if (isMarkdown && typeof marked !== "undefined") {
    const div = document.createElement("div");
    div.className = "modal-markdown";
    div.innerHTML = marked.parse(text, { breaks: false, gfm: true });
    div.querySelectorAll("a").forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    body.appendChild(div);
  } else {
    const pre = document.createElement("pre");
    pre.className = "log-popover-pre";
    pre.textContent = text;
    body.appendChild(pre);
  }

  pop.appendChild(hdr);
  pop.appendChild(body);
  document.body.appendChild(pop);

  const rect = rowEl.getBoundingClientRect();
  const popH = 280;
  const popW = Math.min(520, window.innerWidth - 32);
  let top  = rect.top - popH - 6;
  if (top < 10) top = rect.bottom + 6;
  let left = rect.left;
  if (left + popW > window.innerWidth - 12) left = window.innerWidth - popW - 12;
  pop.style.top   = `${top}px`;
  pop.style.left  = `${left}px`;
  pop.style.width = `${popW}px`;

  setTimeout(() => {
    document.addEventListener("click", function dismiss(e) {
      if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener("click", dismiss); }
    });
  }, 0);
}
