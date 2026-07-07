/**
 * js/main.js — Hermes Console entry point (ES module).
 */

import { store, metaFor }           from "./state/store.js";
import { createSSEConnection }      from "./api/sse.js";
import * as api                     from "./api/client.js";
import { registerPoll, initPollVisibility, setSseOnline } from "./util/pollScheduler.js";
import { instrumentFetch }          from "./util/debugLog.js";

import { setupHealthPanel, handleHealthInit, handleHealthAlert } from "./views/health.js";
import { renderUsage }              from "./views/usage.js";
import { buildAgentCards, setupAgentCardActions, updateAgentCard } from "./views/agents.js";
import {
  mergeKanbanCard, renderKanbanBoard, renderKanbanPills,
  setupKanbanCardActions, archiveAllDone, openCardDrawer, closeCardDrawer,
  refreshCardDrawer, shouldPollOpenDrawer,
} from "./views/kanban.js";
import {
  setupFeedScroll, setFeedFilter, clearFeed,
  appendFeedEvent, attachToolResult,
} from "./views/feed.js";
import { syncFilesFromServer, addFileToRegistry, openFileViewer, closeFileViewer, setupFileModal } from "./views/files.js";
import { loadInitialLogs, appendLogLine, switchLog } from "./views/logs.js";
import { renderActivity }           from "./views/activity.js";
import { upsertPromptTrace, renderPromptTraces } from "./views/traces.js";
import { fetchAndRenderBackupStatus, runBackup }  from "./views/backup.js";
import { loadCronJobs, cronAction, setupCronActions } from "./views/cron.js";
import {
  openSettings, closeSettings, toggleSettings,
  switchSettingsTab, saveSettings, applyAppearance,
  applyPanelVisibility, restorePrefsOnLoad, applyPerformancePrefs, applyDebugPrefs,
  onPollPresetChange,
} from "./views/settings.js";
import { initDebugPanel, refreshServerDebugLog, clearClientDebugLogView } from "./views/debug.js";
import {
  initResizeDispatcher, setupSidebarResizers, setupCenterResizers,
  setupCollapsiblePanels, toggleSidebar, restoreSidebarCollapse,
  relayoutSidebarLayouts,
  updateNavbarClock, setMonitorConnectionState,
  showConnStatus, hideConnStatus, loadInfo, openLogViewer,
} from "./views/layout.js";

instrumentFetch();

// ── SSE event dispatcher ──────────────────────────────────────────────────────

function handleSSEEvent(ev) {
  switch (ev.type) {
    case "kanban_board":
      store.kanbanBoard = ev;
      renderKanbanBoard();
      break;

    case "kanban_card_update":
      mergeKanbanCard(ev.card);
      renderKanbanBoard();
      if (store.openCardId === ev.card?.id) {
        store.openCardStatus = ev.card.status;
        refreshCardDrawer(ev.card.id, { quiet: true });
      }
      appendFeedEvent({
        type:   "agent_event",
        agent:  ev.card.assignee || store.orchestratorId,
        ts:     "",
        kind:   "delegation",
        title:  `[${ev.card.id}] ${metaFor(ev.card.assignee).emoji} ${metaFor(ev.card.assignee).label} · ${ev.prev_status ? `${ev.prev_status} → ${ev.card.status}` : ev.card.status}`,
        detail: ev.card.title,
        full:   "Click the card on the board to see the full handoff.",
      }, openFileViewer, openLogViewer);
      break;

    case "agent_status":
      updateAgentCard(ev.agent, {
        active: ev.active, last_seen: ev.last_seen, session: ev.session,
      });
      break;

    case "agent_event":
      if (ev.kind === "tool_result" && ev.call_id && attachToolResult(ev, openLogViewer)) {
        updateAgentCard(ev.agent, { lastAction: ev.title });
        break;
      }
      appendFeedEvent(ev, openFileViewer, openLogViewer);
      updateAgentCard(ev.agent, { lastAction: ev.title });
      break;

    case "log_line":
      appendLogLine(ev.source, ev.level, ev.text);
      break;

    case "file_event":
      addFileToRegistry(ev, true, openCardDrawer);
      break;

    case "health_init":
      handleHealthInit(ev);
      break;

    case "health_alert":
      handleHealthAlert(ev);
      break;

    case "usage_update":
      renderUsage(ev);
      break;

    case "prompt_trace_init":
      store.promptTraces = ev.traces || [];
      renderPromptTraces(openLogViewer);
      break;

    case "prompt_trace_update":
      upsertPromptTrace(ev.trace);
      renderPromptTraces(openLogViewer);
      break;

    case "heartbeat":
    case "backlog_begin":
    case "backlog_end":
      break;
  }
}

// ── Status poller ─────────────────────────────────────────────────────────────

async function fetchStatus() {
  try {
    const data = await api.fetchStatus();
    if (data.agents) {
      Object.entries(data.agents).forEach(([id, s]) => updateAgentCard(id, s));
    }
    if (data.kanban?.counts) {
      store.kanbanBoard.counts = data.kanban.counts;
      renderKanbanPills();
    }
  } catch (_) {}
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

restorePrefsOnLoad();

document.addEventListener("DOMContentLoaded", async () => {
  try {
    const data = await api.fetchAgents();
    if (data.orchestrator) store.orchestratorId = data.orchestrator;
    (data.agents || []).forEach(a => {
      store.AGENT_META[a.id] = { emoji: a.emoji, color: a.color, label: a.label };
      if (!store.AGENT_IDS.includes(a.id)) store.AGENT_IDS.push(a.id);
      if (!store.agentState[a.id])
        store.agentState[a.id] = { active: false, last_seen: null, session: null, lastAction: "" };
    });
  } catch (_) {}
  buildAgentCards();

  initResizeDispatcher();
  setupCollapsiblePanels();
  restoreSidebarCollapse();
  relayoutSidebarLayouts();
  setupKanbanCardActions();
  setupAgentCardActions();
  setupCronActions();
  setupHealthPanel();
  setupFileModal();
  setupFeedScroll();
  initDebugPanel();
  setupSidebarResizers("left-sidebar",  ["agents-panel", "files-panel", "backup-panel"],
    "hermes-monitor:left-heights",  [0.45, 0.35, 0.20]);
  setupSidebarResizers("right-sidebar", ["usage-panel", "prompt-trace-panel", "activity-panel"],
    "hermes-monitor:right-heights", [1/3, 1/3, 1/3]);
  setupCenterResizers();

  const sse = createSSEConnection({
    url:         "/api/events",
    onEvent:     handleSSEEvent,
    onOpen:      () => {
      setSseOnline(true);
      setMonitorConnectionState("online");
      showConnStatus("Connected", "ok");
      setTimeout(hideConnStatus, 2000);
    },
    onClose:     () => {
      setSseOnline(false);
      setMonitorConnectionState("offline");
    },
    onReconnect: (delayMs) => showConnStatus(`Reconnecting in ${Math.round(delayMs / 1000)}s…`, "error"),
  });
  sse.connect();

  initPollVisibility();

  fetchStatus();
  fetchAndRenderBackupStatus();
  (async () => { try { const d = await api.fetchUsage();        renderUsage(d); }          catch (_) {} })();
  (async () => { try { const d = await api.fetchActivity(80);   renderActivity(d, openLogViewer); } catch (_) {} })();
  (async () => { try { const d = await api.fetchPromptTraces(); store.promptTraces = d.traces || []; renderPromptTraces(openLogViewer); } catch (_) {} })();
  (async () => { try { const d = await api.fetchKanban();       if (d) { store.kanbanBoard = d; renderKanbanBoard(); } } catch (_) {} })();
  (async () => { try { const d = await api.fetchFiles();        syncFilesFromServer(d.files || []); } catch (_) {} })();
  loadInfo();
  loadCronJobs();
  loadInitialLogs("gateway");
  loadInitialLogs("imagine");

  registerPoll({ id: "status",   baseKey: "status",   fn: fetchStatus, sseBackoff: true });
  registerPoll({ id: "backup",   baseKey: "backup",   panelKey: "backup",   fn: fetchAndRenderBackupStatus });
  registerPoll({ id: "info",     baseKey: "info",     fn: loadInfo, always: true, fallbackMs: 30000 });
  registerPoll({ id: "cron",     baseKey: "cron",     panelKey: "cron",     fn: loadCronJobs });
  registerPoll({
    id: "usage", baseKey: "usage", panelKey: "usage", sseBackoff: true,
    fn: async () => { try { renderUsage(await api.fetchUsage()); } catch (_) {} },
  });
  registerPoll({
    id: "activity", baseKey: "activity", panelKey: "activity",
    fn: async () => { try { renderActivity(await api.fetchActivity(80), openLogViewer); } catch (_) {} },
  });
  registerPoll({
    id: "traces", baseKey: "traces", panelKey: "traces", sseBackoff: true,
    fn: async () => {
      try {
        store.promptTraces = (await api.fetchPromptTraces()).traces || [];
        renderPromptTraces(openLogViewer);
      } catch (_) {}
    },
  });
  registerPoll({
    id: "files", baseKey: "files", panelKey: "files",
    fn: async () => { try { syncFilesFromServer((await api.fetchFiles()).files || []); } catch (_) {} },
  });
  registerPoll({
    id: "drawer", baseKey: "drawer",
    fn: () => {
      if (shouldPollOpenDrawer()) refreshCardDrawer(store.openCardId, { quiet: true });
    },
  });
  registerPoll({ id: "debugLog", baseKey: "debugLog", fn: refreshServerDebugLog, always: true, fallbackMs: 2000 });

  updateNavbarClock();
  setInterval(updateNavbarClock, 1000);
});

Object.assign(window, {
  toggleSidebar,
  setFeedFilter,
  clearFeed,
  archiveAllDone,
  openCardDrawer,
  closeCardDrawer,
  closeFileViewer,
  openFileViewer,
  switchLog,
  loadInitialLogs,
  loadCronJobs,
  cronAction,
  runBackup,
  toggleSettings,
  openSettings,
  closeSettings,
  switchSettingsTab,
  saveSettings,
  applyAppearance,
  applyPanelVisibility,
  applyPerformancePrefs,
  applyDebugPrefs,
  onPollPresetChange,
  clearClientDebugLogView,
  refreshServerDebugLog,
});
