# Hermes Console

Real-time control panel dashboard for the Hermes multi-agent system. Watches session
JSONL files, kanban state, live gateway/agent logs, and output directories across
all active profiles.

Drop this folder into any Hermes workflow — it discovers agents, reads the right
paths, and adapts to Docker or native gateways entirely through a single `.env`
file.

---

## What it shows

| Panel | What you see |
|---|---|
| **Agents** | Per-agent active/idle dot, current session ID, start/stop/restart controls |
| **Activity feed** | Assistant turns, tool calls, file writes, delegations — live via SSE |
| **Kanban board** | Inter-agent task cards (created → running → blocked → done) |
| **Generated files** | Every file dropped into `workspace/<agent>/output/` |
| **Token usage** | Live API-call tracking, cost estimation, daily rollups — only shows providers that are configured |
| **Health** | API credit status, gateway/platform connectivity, stalled tasks |
| **Prompt traces** | Slack inbound timeline with per-prompt cost breakdown |

---

## Quick start

### 1. Copy and fill in your config

```bash
cp .env.example .env
# Edit .env — set HERMES_DIR at minimum
```

The only required value is `HERMES_DIR`. Everything else has a working default.

### 2. Start

```bash
./start.sh
# or: python3 server.py
open http://localhost:7979   # or whatever HERMES_WEBUI_PORT is set to
```

No pip dependencies — stdlib only.

---

## Configuration — `.env`

All settings live in `.env` in this directory. Copy `.env.example` to get started:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `HERMES_DIR` | `~/.hermes` | Hermes home directory. All profiles, kanban DB, and logs are read from here. |
| `HERMES_ORCHESTRATOR` | `sage` | Root agent name — the one whose sessions live directly in `HERMES_DIR` rather than under `profiles/<name>`. Change this if your workflow uses a different name for the default agent. |
| `HERMES_WEBUI_PORT` | `7979` | HTTP port the dashboard listens on. |
| `HERMES_WEBUI_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` only when intentionally exposing the console on a network. |
| `HERMES_MONITOR_DB` | `<web-ui>/monitor.db` | SQLite file for API usage events and prompt traces. |
| `HERMES_DOCKER_CONTAINER` | _(unset)_ | Docker container name. When set, start/stop/restart buttons use `docker` commands instead of `launchctl`. Leave blank for a native macOS launchd-managed gateway. |
| `HERMES_AGENT_PLISTS` | sage/imagine defaults | Optional comma map for native launchd gateways, e.g. `sage=~/Library/LaunchAgents/ai.hermes.gateway.plist`. |
| `HERMES_AGENT_LABELS` | sage/imagine defaults | Optional comma map for launchd labels, e.g. `sage=ai.hermes.gateway`. |
| `HERMES_BACKUP_REPO` | _(unset)_ | Git repo path containing `backup.sh`. Enables the backup panel. Leave blank to hide it. |
| `HERMES_BACKUP_SCRIPT` | _(auto)_ | Path to `backup.sh`. Auto-detected as `HERMES_BACKUP_REPO/backup.sh` if not set. |
| `SLACK_BOT_TOKEN` | _(unset)_ | Slack bot token (`xoxb-…`) for the trace mirror. Falls back to reading `HERMES_DIR/.env`. |
| `SLACK_TRACE_CHANNEL` | _(unset)_ | Channel ID for the Slack trace mirror. Both this and `SLACK_BOT_TOKEN` must be set to enable. |
| `HERMES_TRACE_MODE` | `milestones` | Slack mirror verbosity: `milestones` (quiet) or `verbose` (all tool calls). |
| `HERMES_MEDIA_DIR` | `HERMES_DIR/cache/images` | Optional generated-media directory outside agent workspaces to scan. |
| `HERMES_MEDIA_AGENT` | `imagine` | Agent ID used to attribute files from `HERMES_MEDIA_DIR`; falls back to the orchestrator if absent. |
| `HERMES_WEBUI_WARMUP_FRESHNESS` | `21600` | Seconds of past session events to replay into the activity feed on startup. Set to `0` for a clean feed. |

**Priority order (highest to lowest):** shell environment → `.env.local` → `.env` → built-in defaults.

Use `.env.local` for machine-specific overrides that you don't want to commit.

---

## Dynamic agent discovery

Agents are discovered automatically at startup from `HERMES_DIR/profiles/`.
**You do not need to edit `server.py` or `app.js` when you add a new profile.**

- The orchestrator (`HERMES_ORCHESTRATOR`) is always included as the root agent
- Any `profiles/<name>/` subdirectory is picked up automatically
- Well-known agents (sage, imagine, ink, recon, signal, anton) get fixed colors and emojis; new profiles get the next slot from a built-in palette
- Native launchd gateway controls can be mapped per workflow with `HERMES_AGENT_PLISTS` and `HERMES_AGENT_LABELS`
- Docker worker controls discover active worker PIDs from `kanban.db` instead of hardcoding agent locations

The frontend bootstraps the agent list from `/api/agents` on page load.

---

## Dropping into a new Hermes workflow

1. Copy this `web-ui/` folder into the new project
2. `cp .env.example .env`
3. Set `HERMES_DIR` to your Hermes home (native path or Docker bind-mount)
4. `./start.sh` or `python3 server.py`

Agent discovery, log paths, kanban DB, and API provider detection all resolve
from `HERMES_DIR` automatically. Nothing else to change.

---

## Slack trace mirror

When `SLACK_BOT_TOKEN` and `SLACK_TRACE_CHANNEL` are set, the console mirrors
high-signal events to Slack.

**Milestones mode** (default): user ask, kanban create/claim/done, blocked, delivered.

**Incidents** always post regardless of mode: tool errors, API exhaustion, delivery failures.

Full tool chains, search calls, and per-prompt costs stay in the dashboard only.

---

## Settings Panel

Click the **⚙** gear button in the top-right corner of the header to open the settings slide-out panel.

### Connection tab

Writes settings to `.env.local` (in the `web-ui/` directory). Restart the server
after saving env-backed settings so the Python process reloads them.

| Field | Env var | Description |
|---|---|---|
| HERMES_DIR | `HERMES_DIR` | Path to the Hermes home directory |
| Orchestrator | `HERMES_ORCHESTRATOR` | Root agent name (default: `sage`) |
| Docker Container | `HERMES_DOCKER_CONTAINER` | Container name, or blank for launchctl mode |
| Web UI Host | `HERMES_WEBUI_HOST` | Bind address (`127.0.0.1` by default) |
| Web UI Port | `HERMES_WEBUI_PORT` | HTTP port (requires restart to take effect) |
| Agent Launchd Plists | `HERMES_AGENT_PLISTS` | Optional comma map of agent → plist path |
| Agent Launchd Labels | `HERMES_AGENT_LABELS` | Optional comma map of agent → launchd label |
| Backup Repo Path | `HERMES_BACKUP_REPO` | Git repo containing `backup.sh` |

Click **Save** to write `.env.local`; restart the console to apply server-side
changes.

### Appearance tab

Stored in `localStorage` — no server round-trip needed.

- **Dark Mode** — applies `.dark-mode` CSS class to `<body>`
- **Compact Mode** — tighter panel spacing via `.compact-mode` CSS class
- **Visible Panels** — show/hide individual sidebar panels (Usage, Backup, Prompt Traces, Tool Activity, Files, Cron Jobs)

---

## HTTP API

| Method | Path | Returns |
|---|---|---|
| GET | `/api/agents` | All discovered agents with id, label, emoji, color, root, is_extra |
| GET | `/api/status` | Agent active/idle state + kanban counts, worker PIDs |
| GET | `/api/events` | SSE stream of all live events |
| GET | `/api/info` | Server info: hermes_dir, orchestrator, docker_container, version, worker PIDs |
| GET | `/api/settings` | Current env-driven config |
| POST | `/api/settings` | Write settings to `.env.local` (requires restart) |
| GET | `/api/cron` | List all scheduled cron jobs |
| POST | `/api/cron/<id>/<pause\|resume\|run>` | Control a cron job |
| GET | `/api/kanban` | Full kanban board snapshot |
| GET | `/api/kanban/card/<id>` | Single card detail with events, runs, comments, log tail |
| GET | `/api/files` | List of generated output files |
| GET | `/api/file?path=<enc>` | File content (image, text, HTML) |
| GET | `/api/usage` | Token usage ledger — only configured providers returned |
| GET | `/api/activity` | Tool activity rows from the Postgres activity table |
| GET | `/api/prompt-traces` | Slack prompt trace history |
| GET | `/api/health` | Active health issues |
| GET | `/api/logs?source=<agent>&lines=<n>` | Log tail for any agent |
| GET | `/api/backup/status` | Git backup repo status |
| POST | `/api/backup/run` | Run `backup.sh` |
| POST | `/api/kanban/archive-done` | Archive all done cards |
| POST | `/api/kanban/card/<id>/archive` | Archive one card; falls back to direct kanban.db update if Hermes CLI cannot archive it |
| POST | `/api/kanban/card/<id>/cancel` | Reclaim + archive a card; falls back to direct kanban.db update for dispatcher-owned/lab cards |
| POST | `/api/agent/<id>/<start\|stop\|restart>` | Control a gateway (launchd or Docker) |

---

## File layout

```
server.py            HTTP + SSE backend. Loads .env on startup, then spawns a
                     Watcher thread that polls JSONL, logs, state.db, kanban.db.
.env.example         Template config — copy to .env.
start.sh             Start the console in the background.
restart.sh           Restart the console (or start if stopped).
stop.sh              Stop the console (gateways unchanged).
_lib.sh              Shared helpers for start/stop/restart.
watch_agents.py      Standalone terminal trajectory viewer (tails session JSONL).
static/
  index.html         Single-page app shell.
  app.js             Vanilla JS. Bootstraps agent list from /api/agents on load.
  style.css          Dark theme.
  marked.min.js      Markdown renderer for kanban card bodies.
monitor.db           SQLite usage ledger (auto-created, path set by HERMES_MONITOR_DB).
```

---

## Run as a launchd service (macOS)

Configuration is read from `web-ui/.env` at startup — no `EnvironmentVariables`
block needed in the plist.

Drop the following at `~/Library/LaunchAgents/sh.hermes.webui.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>sh.hermes.webui</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/web-ui/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/web-ui</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/hermes-webui.log</string>
  <key>StandardErrorPath</key><string>/tmp/hermes-webui.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/sh.hermes.webui.plist
```
