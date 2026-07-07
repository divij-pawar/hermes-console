"""
hermes_console.config
~~~~~~~~~~~~~~~~~~~~~
Environment bootstrap, all path constants, and agent discovery.
Everything here is read-only after module import; no threads or IO happen at
call-time (only at module load).
"""

import os

# ── .env bootstrap ─────────────────────────────────────────────────────────────
# Load .env (and .env.local for local overrides) from the web-ui directory
# before reading any os.environ.get() calls.  Shell environment always wins —
# values already set in the environment are never overwritten.


def _bootstrap_env() -> None:
    _dir = os.path.dirname(os.path.abspath(__file__))
    # Walk up from src/hermes_console/ → src/ → web-ui/
    _root = os.path.dirname(os.path.dirname(_dir))
    for _name in (".env", ".env.local"):
        _path = os.path.join(_root, _name)
        try:
            with open(_path, encoding="utf-8") as _f:
                for _raw in _f:
                    _line = _raw.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _key, _val = _line.split("=", 1)
                    _key = _key.strip()
                    _val = _val.strip().strip("'\"")
                    if _key and _key not in os.environ:
                        os.environ[_key] = _val
        except OSError:
            pass


_bootstrap_env()

# ── Paths ──────────────────────────────────────────────────────────────────────
HERMES_DIR          = os.environ.get("HERMES_DIR", os.path.expanduser("~/.hermes"))
HERMES_ORCHESTRATOR = os.environ.get("HERMES_ORCHESTRATOR", "sage")
HERMES_WEBUI_HOST   = os.environ.get("HERMES_WEBUI_HOST", "127.0.0.1")
PROFILES_DIR  = os.path.join(HERMES_DIR, "profiles")
WORKSPACE_DIR = os.path.join(HERMES_DIR, "workspace")
LOGS_DIR      = os.path.join(HERMES_DIR, "logs")
KANBAN_DB     = os.path.join(HERMES_DIR, "kanban.db")
KANBAN_LOGS   = os.path.join(HERMES_DIR, "kanban", "logs")

# Resolve the web-ui directory (two levels up from this file)
SCRIPT_DIR  = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MONITOR_DB  = os.environ.get("HERMES_MONITOR_DB", os.path.join(SCRIPT_DIR, "monitor.db"))
STATIC_DIR  = os.path.join(SCRIPT_DIR, "static")
PORT        = int(os.environ.get("PORT") or os.environ.get("HERMES_WEBUI_PORT", "7979"))

# ── Backup ──────────────────────────────────────────────────────────────────────
_SF_AGENTS_REPO_FILE = os.path.join(HERMES_DIR, ".sf_agents_repo")


def _read_sf_agents_repo_file() -> str:
    try:
        with open(_SF_AGENTS_REPO_FILE, encoding="utf-8") as f:
            path = f.read().strip()
        if path and os.path.isdir(path):
            return path
    except OSError:
        pass
    return ""


def _resolve_backup_paths() -> tuple[str, str]:
    """Return (repo_dir, script_path); empty strings when backup is unavailable."""
    repo = (
        os.environ.get("HERMES_BACKUP_REPO", "").strip()
        or os.environ.get("SF_AGENTS_REPO", "").strip()
        or _read_sf_agents_repo_file()
    )
    script = os.environ.get("HERMES_BACKUP_SCRIPT", "").strip()
    if not script and repo:
        candidate = os.path.join(repo, "backup.sh")
        if os.path.isfile(candidate):
            script = candidate
    return repo, script


BACKUP_REPO, BACKUP_SCRIPT = _resolve_backup_paths()

# ── Agent metadata & discovery ─────────────────────────────────────────────────
# Well-known agents get fixed colors and emojis.  Any agent discovered in
# profiles/ but not listed here gets the next entry from _AGENT_PALETTE.
_KNOWN_AGENT_META: dict[str, dict] = {
    "sage":    {"emoji": "🧭", "color": "#58a6ff", "label": "SAGE"},
    "imagine": {"emoji": "🎨", "color": "#bc8cff", "label": "IMAGINE"},
    "ink":     {"emoji": "🖊️",  "color": "#3fb950", "label": "INK"},
    "recon":   {"emoji": "🔎", "color": "#f0883e", "label": "RECON"},
    "signal":  {"emoji": "📡", "color": "#79c0ff", "label": "SIGNAL"},
    "anton":   {"emoji": "🔨", "color": "#e3b341", "label": "ANTON"},
}
_AGENT_PALETTE = [
    ("#a5d6ff", "⚡"), ("#f778ba", "🔬"), ("#56d364", "🎯"),
    ("#ff7b72", "💡"), ("#ffa657", "🛠️"), ("#d2a8ff", "🌐"),
]


def _discover_agents(hermes_dir: str) -> list[str]:
    """Return agent IDs present in *hermes_dir*.

    Always includes the orchestrator as the default-profile root.  Any
    subdirectory found under profiles/ is added automatically.
    """
    agents = [HERMES_ORCHESTRATOR]
    profiles = os.path.join(hermes_dir, "profiles")
    if os.path.isdir(profiles):
        for name in sorted(os.listdir(profiles)):
            if os.path.isdir(os.path.join(profiles, name)) and name not in agents:
                agents.append(name)
    return agents


def _agent_log_paths(agent_id: str, hermes_dir: str) -> dict[str, str]:
    """Return {gateway: path, agent: path} for *agent_id* inside *hermes_dir*."""
    if agent_id == HERMES_ORCHESTRATOR:
        logs = os.path.join(hermes_dir, "logs")
    else:
        logs = os.path.join(hermes_dir, "profiles", agent_id, "logs")
    return {
        "gateway": os.path.join(logs, "gateway.log"),
        "agent":   os.path.join(logs, "agent.log"),
    }


# ── Primary + extra Hermes homes ───────────────────────────────────────────────
# _AGENT_HOME_MAP overrides which hermes_dir to use for agents sourced from
# HERMES_EXTRA_HOME (e.g. the Docker lab bind-mount on the host).
_AGENT_HOME_MAP: dict[str, str] = {}

AGENT_IDS: list[str] = _discover_agents(HERMES_DIR)

HERMES_EXTRA_HOME = os.environ.get("HERMES_EXTRA_HOME", "").strip()
if HERMES_EXTRA_HOME and os.path.isdir(HERMES_EXTRA_HOME):
    for _eid in _discover_agents(HERMES_EXTRA_HOME):
        if _eid not in AGENT_IDS:
            AGENT_IDS.append(_eid)
            _AGENT_HOME_MAP[_eid] = HERMES_EXTRA_HOME


def _agent_home(agent_id: str) -> str:
    """Return the Hermes home directory for *agent_id*."""
    return _AGENT_HOME_MAP.get(agent_id, HERMES_DIR)


def _agent_root(agent_id: str) -> str:
    """Return the profile root directory for *agent_id*."""
    home = _agent_home(agent_id)
    if agent_id == HERMES_ORCHESTRATOR and home == HERMES_DIR:
        return HERMES_DIR
    if agent_id == HERMES_ORCHESTRATOR:
        return home
    return os.path.join(home, "profiles", agent_id)


def _agent_output_dir(agent_id: str) -> str:
    home = _agent_home(agent_id)
    return os.path.join(home, "workspace", agent_id, "output")


# Per-agent live log sources.  Built from AGENT_IDS so any newly discovered
# profile is included automatically.
AGENT_LIVE_LOGS: dict[str, dict[str, str]] = {
    aid: _agent_log_paths(aid, _agent_home(aid))
    for aid in AGENT_IDS
}

# Alias for the primary gateway log.
GATEWAY_LOG = AGENT_LIVE_LOGS[HERMES_ORCHESTRATOR]["gateway"]

AGENT_OUTPUT_DIRS: dict[str, str] = {
    aid: _agent_output_dir(aid)
    for aid in AGENT_IDS
}

# ── Media ──────────────────────────────────────────────────────────────────────
MEDIA_DIR = os.path.expanduser(
    os.environ.get("HERMES_MEDIA_DIR", os.path.join(HERMES_DIR, "cache", "images"))
)
MEDIA_AGENT = os.environ.get("HERMES_MEDIA_AGENT", "imagine").strip()

# ── Polling ────────────────────────────────────────────────────────────────────
POLL_INTERVAL = float(os.environ.get("HERMES_WEBUI_POLL", "0.1"))

# ── File registry limits ───────────────────────────────────────────────────────
FILE_REGISTRY_MAX_AGE_SEC = float(os.environ.get("HERMES_FILES_MAX_AGE_DAYS", "14")) * 86400
FILE_REGISTRY_MAX_ENTRIES = int(os.environ.get("HERMES_FILES_MAX_ENTRIES", "80"))
FILE_POLL_CACHE_IMAGES = os.environ.get("HERMES_FILES_POLL_CACHE", "1").strip().lower() not in (
    "0", "false", "no",
)

# ── Slack trace ────────────────────────────────────────────────────────────────
def _read_env_file(path: str) -> dict[str, str]:
    """Tiny .env parser — KEY=VALUE per line, ignores comments and blanks."""
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    out[k] = v
    except OSError:
        pass
    return out


_sage_env = _read_env_file(os.path.join(HERMES_DIR, ".env"))
SLACK_TRACE_TOKEN   = (os.environ.get("SLACK_TRACE_TOKEN")
                       or _sage_env.get("SLACK_BOT_TOKEN", ""))
SLACK_TRACE_CHANNEL = (os.environ.get("SLACK_TRACE_CHANNEL")
                       or _sage_env.get("SLACK_TRACE_CHANNEL", ""))
TRACE_ENABLED = bool(SLACK_TRACE_TOKEN and SLACK_TRACE_CHANNEL)
TRACE_MODE = os.environ.get("HERMES_TRACE_MODE", "milestones").strip().lower()
