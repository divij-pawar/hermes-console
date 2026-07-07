/**
 * state/store.js — Centralised mutable state for the dashboard.
 *
 * All global variables that were scattered across app.js now live here as
 * properties of the single exported `store` object.  Mutation is intentional
 * and explicit — no reactive framework — but having one owner makes tracing
 * data flow dramatically easier.
 *
 * Usage:
 *   import { store } from '../state/store.js';
 *   store.kanbanBoard = newBoard;
 *   const ids = store.AGENT_IDS;
 */

const _PALETTE = [
  { color: "#58a6ff", emoji: "🧭" }, { color: "#bc8cff", emoji: "🎨" },
  { color: "#3fb950", emoji: "🖊️"  }, { color: "#f0883e", emoji: "🔎" },
  { color: "#79c0ff", emoji: "📡" }, { color: "#e3b341", emoji: "🔨" },
  { color: "#a5d6ff", emoji: "⚡" }, { color: "#f778ba", emoji: "🔬" },
  { color: "#56d364", emoji: "🎯" }, { color: "#ff7b72", emoji: "💡" },
];

export const store = {
  // ── Agents ──────────────────────────────────────────────────────────────────
  AGENT_IDS:     /** @type {string[]} */ ([]),
  AGENT_META:    /** @type {Record<string, {emoji:string,color:string,label:string}>} */ ({}),
  orchestratorId: "sage",
  agentState:    /** @type {Record<string, {active:boolean,last_seen:string|null,session:string|null,lastAction:string}>} */ ({}),

  // Palette auto-assignment for unknown agents.
  _paletteIdx: 0,

  // ── UI ───────────────────────────────────────────────────────────────────────
  currentLogSource: "gateway",
  feedCount:   0,
  atBottom:    true,
  feedFilter:  "all",

  /** @type {Map<string, Element>} */
  feedRowsByCallId: new Map(),

  latestToolActivityRows: /** @type {any[]} */ ([]),
  promptTraces:           /** @type {any[]} */ ([]),

  // ── Kanban ───────────────────────────────────────────────────────────────────
  kanbanBoard: { tasks: [], counts: {}, available: false },
  openCardId:     null,
  openCardStatus: null,

  // ── Performance / connection ─────────────────────────────────────────────────
  sseOnline:  false,
  uiDragging: false,
  rendersPaused: false,

  // ── Files ────────────────────────────────────────────────────────────────────
  fileRegistry: /** @type {any[]} */ ([]),
  fileCount: 0,

  // ── SSE ──────────────────────────────────────────────────────────────────────
  sseSource:      /** @type {EventSource|null} */ (null),
  reconnectDelay: 3000,
  reconnectTimer: /** @type {ReturnType<typeof setTimeout>|null} */ (null),

  // ── Health ───────────────────────────────────────────────────────────────────
  issues: /** @type {Map<string, any>} */ (new Map()),

  DISMISSED_ISSUES_KEY: "hermes-monitor:dismissed-issues",
  get dismissedIssues() {
    try {
      return new Set(JSON.parse(sessionStorage.getItem(this.DISMISSED_ISSUES_KEY) || "[]"));
    } catch {
      return new Set();
    }
  },
};

/**
 * Look up or auto-register agent metadata.
 * Mirrors the old _metaFor() global function.
 */
export function metaFor(agentId) {
  if (store.AGENT_META[agentId]) return store.AGENT_META[agentId];
  const slot = _PALETTE[store._paletteIdx % _PALETTE.length];
  store._paletteIdx++;
  store.AGENT_META[agentId] = {
    emoji: slot.emoji,
    color: slot.color,
    label: agentId.toUpperCase(),
  };
  if (!store.AGENT_IDS.includes(agentId)) {
    store.AGENT_IDS.push(agentId);
    store.agentState[agentId] = { active: false, last_seen: null, session: null, lastAction: "" };
  }
  return store.AGENT_META[agentId];
}

export function persistDismissedIssues(dismissed) {
  sessionStorage.setItem(store.DISMISSED_ISSUES_KEY, JSON.stringify([...dismissed]));
}
