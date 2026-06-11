# Hermes Web UI

Lightweight dashboard for watching Hermes agents (Sage, Imagine, future
specialists) in real time. Ported from the OpenClaw Monitor.

## What it shows

- **Agents panel** — per-agent active/idle status, current session id, last seen
- **Activity feed** — assistant turns, tool calls, file writes, delegation
  events streamed live via SSE
- **Generated Files** — every file dropped into `~/.hermes/workspace/<agent>/output/`
- **Logs** — tail of `~/.hermes/logs/gateway.log` and the relay stub log
- **Relay sidecar** — start/stop/restart button. In Hermes the relay is a
  no-op stub (Kanban handles inter-bot work). It's kept so the UI control
  still has a process to manage.
- **Config backup** — syncs `~/.hermes/` into the `sf_agents` git repo via
  `backup.sh`. Auto-detects `~/Documents/sf_agents` when present. Override
  with `HERMES_BACKUP_REPO` / `HERMES_BACKUP_SCRIPT`. Not the same as
  `hermes backup` (full-home zip).

## Start

```bash
# Foreground
python3 ~/.hermes/web-ui/server.py

# Browse to
open http://localhost:7979
```

The server uses only Python stdlib — no dependencies.

## Slack trace channel (`#hermes_trace`)

The Monitor mirrors high-signal workflow events to Slack when `SLACK_BOT_TOKEN`
and `SLACK_TRACE_CHANNEL` are set in `~/.hermes/.env`.

| Env | Default | Effect |
|-----|---------|--------|
| `HERMES_TRACE_MODE` | `milestones` | Quiet milestone posts only |
| `HERMES_TRACE_MODE` | `verbose` | Legacy: per-file writes, status churn, delegations |

**Milestones mode** posts: user ask (inbound), kanban create/claim/rich done,
blocked, delivered to front. **Incidents** always post (even in milestone mode):
tool errors, `finish_reason=error`, empty-response exhaustion, degraded/gaps on
done, Slack delivery failures.

Full tool chains, Tavily/x_search lines, and prompt-trace costs stay in the
Monitor UI only — not duplicated in Slack.

Restart Monitor after changing trace mode or `server.py`:

```bash
~/.hermes/web-ui/restart.sh
# or: ~/.hermes/web-ui/start.sh   (wrapper to restart.sh)
```

`restart.sh` restarts the Monitor if it is already running, or starts it if not.
Gateways are untouched. For the full fleet (gateways + Monitor), use
`start-fleet.sh`.

## Architecture

```
server.py         HTTP + SSE on port 7979. Spawns a Watcher thread that polls
                  session jsonl files, output dirs, and the gateway log.
relay.py          No-op idle process. Hermes uses Kanban for cross-bot
                  delegation, not a Slack relay. This file is kept so the
                  UI's start/stop control still has something to manage.
watch_agents.py   Standalone terminal trajectory viewer. Tails the latest
                  session jsonl for a chosen agent.
static/           index.html + app.js + style.css (vanilla JS, no build).
```

## Per-agent paths

| Agent     | Session jsonl                              | Output dir                                    |
|-----------|--------------------------------------------|-----------------------------------------------|
| `sage`    | `~/.hermes/sessions/*.jsonl`               | `~/.hermes/workspace/sage/output/`            |
| `imagine` | `~/.hermes/profiles/imagine/sessions/*.jsonl` | `~/.hermes/workspace/imagine/output/`     |

Adding a new specialist: append to `AGENT_IDS` in `server.py` and the
`AGENT_META` map in `static/app.js`. The agent root resolver
(`_agent_root()`) automatically maps any non-`sage` id to
`~/.hermes/profiles/<id>/`.

## Config backup (sf_agents)

If `~/Documents/sf_agents/backup.sh` exists, the panel works with no env vars.

Optional overrides:

```bash
export HERMES_BACKUP_REPO=~/Documents/sf_agents
export HERMES_BACKUP_SCRIPT=~/Documents/sf_agents/backup.sh
python3 ~/.hermes/web-ui/server.py
```

| Action | Tool |
|--------|------|
| Git config snapshot | Monitor **Backup Now** or `./backup.sh` |
| Restore from git | `cd ~/Documents/sf_agents && ./setup.sh` |
| Full `~/.hermes` zip | `hermes backup` in Terminal |

## Run as a launchd service (macOS)

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
    <string>/Users/divijpawar/.hermes/web-ui/server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HERMES_BACKUP_REPO</key><string>/Users/divijpawar/Documents/sf_agents</string>
    <key>HERMES_BACKUP_SCRIPT</key><string>/Users/divijpawar/Documents/sf_agents/backup.sh</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/divijpawar/.hermes/logs/webui.log</string>
  <key>StandardErrorPath</key><string>/Users/divijpawar/.hermes/logs/webui.log</string>
</dict>
</plist>
```

Then:

```bash
launchctl load ~/Library/LaunchAgents/sh.hermes.webui.plist
```
