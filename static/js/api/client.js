/**
 * api/client.js — Thin fetch wrappers for every Hermes Console endpoint.
 *
 * Each function returns a plain Promise<data> (the parsed JSON).
 * Error handling is intentionally minimal — callers decide what to show.
 * There is NO DOM access here; that separation is the whole point.
 */

const BASE = "";  // same origin — no prefix needed

async function _get(path) {
  const res = await fetch(BASE + path);
  return res.json();
}

async function _post(path, body = {}) {
  const res = await fetch(BASE + path, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify(body),
  });
  return res.json();
}

// ── Read endpoints ─────────────────────────────────────────────────────────────

/** GET /api/agents — orchestrator + agent metadata list */
export async function fetchAgents() {
  return _get("/api/agents");
}

/** GET /api/status — per-agent gateway/worker status */
export async function fetchStatus() {
  return _get("/api/status");
}

/** GET /api/usage — token/cost snapshot */
export async function fetchUsage() {
  return _get("/api/usage");
}

/** GET /api/kanban — board summary */
export async function fetchKanban() {
  return _get("/api/kanban");
}

/** GET /api/kanban/card/<id> — card detail */
export async function fetchKanbanCard(taskId) {
  return _get(`/api/kanban/card/${taskId}`);
}

/** GET /api/backup/status */
export async function fetchBackupStatus() {
  return _get("/api/backup/status");
}

/** GET /api/activity?limit=N */
export async function fetchActivity(limit = 80) {
  return _get(`/api/activity?limit=${limit}`);
}

/** GET /api/prompt-traces */
export async function fetchPromptTraces() {
  return _get("/api/prompt-traces");
}

/** GET /api/health */
export async function fetchHealth() {
  return _get("/api/health");
}

/** GET /api/files */
export async function fetchFiles() {
  return _get("/api/files");
}

/** GET /api/logs?source=<source>&lines=N */
export async function fetchLogs(source = "gateway", lines = 200) {
  return _get(`/api/logs?source=${encodeURIComponent(source)}&lines=${lines}`);
}

/** GET /api/cron */
export async function fetchCron() {
  return _get("/api/cron");
}

/** GET /api/settings */
export async function fetchSettings() {
  return _get("/api/settings");
}

/** GET /api/info — hermes dir, orchestrator, docker container */
export async function fetchInfo() {
  return _get("/api/info");
}

/** GET /api/console-log?lines=N — tail of server console.log */
export async function fetchConsoleLog(lines = 200) {
  return _get(`/api/console-log?lines=${lines}`);
}

/** GET /api/file?path=<p> — raw file bytes (used for the file viewer) */
export function fileContentUrl(path) {
  return `/api/file?path=${encodeURIComponent(path)}`;
}

// ── Write endpoints ────────────────────────────────────────────────────────────

/** POST /api/backup/run */
export async function runBackup() {
  return _post("/api/backup/run");
}

/** POST /api/kanban/archive-done */
export async function archiveDoneCards() {
  return _post("/api/kanban/archive-done");
}

/** POST /api/kanban/card/<id>/archive */
export async function archiveCard(taskId) {
  return _post(`/api/kanban/card/${taskId}/archive`);
}

/** POST /api/kanban/card/<id>/cancel */
export async function cancelCard(taskId) {
  return _post(`/api/kanban/card/${taskId}/cancel`);
}

/** POST /api/agent/<id>/<action> */
export async function agentLifecycle(agentId, action) {
  return _post(`/api/agent/${agentId}/${action}`);
}

/** POST /api/cron/<id>/<action> */
export async function cronAction(cronId, action) {
  return _post(`/api/cron/${cronId}/${action}`);
}

/** POST /api/settings */
export async function saveSettings(data) {
  return _post("/api/settings", data);
}
