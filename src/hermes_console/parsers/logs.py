from __future__ import annotations

"""
hermes_console.parsers.logs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Live log-tail patterns — each ``(compiled_rx, builder_fn)`` pair in
``LIVE_LOG_PATTERNS`` maps a gateway.log or agent.log line to a feed event
dict (or ``None`` to suppress the entry while still executing side-effects
like token crediting or health-issue management).

Builders import their dependencies lazily to keep import-time cost minimal and
to avoid circular dependency issues.
"""

import re as _re
import time

from hermes_console.config import AGENT_IDS, HERMES_DIR
from hermes_console.parsers.session import STRUCTURED_LOG_TOOLS

# ── Pattern registry ───────────────────────────────────────────────────────────
LIVE_LOG_PATTERNS: list[tuple] = []


def _register_pattern(rx: str, builder) -> None:
    LIVE_LOG_PATTERNS.append((_re.compile(rx), builder))


def _now_hms() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


# ── Pattern builders ───────────────────────────────────────────────────────────

def _inbound_builder(m, aid: str) -> dict:
    from hermes_console.services.usage import _start_prompt
    from hermes_console.services.prompt_trace import _trace_start
    from hermes_console.services.slack_trace import _trace_milestone_inbound
    _start_prompt(aid, m.group("msg"))
    _trace_start(aid, m.group("msg"), m.group("platform"), m.group("user"), m.group("chat"))
    _trace_milestone_inbound(aid, m.group("msg"), m.group("platform"), m.group("chat"))
    return {
        "type":  "agent_event",
        "agent": aid,
        "ts":    _now_hms(),
        "kind":  "user_message",
        "title": m.group("msg")[:120],
        "full":  f"[{m.group('user')}] via {m.group('platform')}\n{m.group('msg')}",
        "live":  True,
    }


_register_pattern(
    r"gateway\.run:\s+inbound message:\s+platform=(?P<platform>\S+)\s+user=(?P<user>.+?)\s+chat=(?P<chat>\S+)\s+msg='(?P<msg>.+?)'",
    _inbound_builder,
)


def _response_ready_builder(m, aid: str) -> dict:
    from hermes_console.services.prompt_trace import _trace_finish
    from hermes_console.services.slack_trace import _trace_milestone_delivered
    detail = f"{m.group('time')}s · {m.group('calls')} API calls · {m.group('chars')} chars"
    _trace_finish(aid, detail, elapsed_s=float(m.group("time")),
                  api_calls=int(m.group("calls")), response_chars=int(m.group("chars")))
    _trace_milestone_delivered(aid, float(m.group("time")), int(m.group("calls")), int(m.group("chars")))
    return {
        "type":  "agent_event",
        "agent": aid,
        "ts":    _now_hms(),
        "kind":  "response",
        "title": f"Slack response ready · {detail}",
        "full":  (f"platform: {m.group('platform')}\nchat: {m.group('chat')}\n"
                  f"elapsed: {m.group('time')}s\napi_calls: {m.group('calls')}\n"
                  f"response_chars: {m.group('chars')}"),
        "live":  True,
    }


_register_pattern(
    r"gateway\.run:\s+response ready:\s+platform=(?P<platform>\S+)\s+chat=(?P<chat>\S+)\s+time=(?P<time>[\d.]+)s\s+api_calls=(?P<calls>\d+)\s+response=(?P<chars>\d+)\s+chars",
    _response_ready_builder,
)


def _tavily_search_builder(m, aid: str):
    from hermes_console.services.usage import _credit_external_tool
    from hermes_console.services.prompt_trace import _trace_add
    from hermes_console.parsers.session import _mark_feed_tool_emitted
    query = m.group("query")
    _credit_external_tool("tavily", aid, "tavily_search", query, detail=f"Tavily search: {query}")
    _trace_add(aid, "tavily.search", query[:120])
    _mark_feed_tool_emitted(aid, "tool_call", "web_search", query)
    return None


def _tavily_extract_builder(m, aid: str):
    from hermes_console.services.usage import _credit_external_tool
    from hermes_console.services.prompt_trace import _trace_add
    from hermes_console.parsers.session import _mark_feed_tool_emitted
    url = m.group("url")
    _credit_external_tool("tavily", aid, "tavily_extract", url, detail=f"Tavily extract: {url}")
    _trace_add(aid, "tavily.extract", url[:120])
    _mark_feed_tool_emitted(aid, "tool_call", "web_extract", url)
    return None


_register_pattern(r"plugins\.web\.tavily\.provider:\s+Tavily search:\s+'(?P<query>.+?)'", _tavily_search_builder)
_register_pattern(r"plugins\.web\.tavily\.provider:\s+Tavily extract request to\s+(?P<url>\S+)", _tavily_extract_builder)


def _tool_completed_builder(m, aid: str):
    name = m.group("name")
    if name in STRUCTURED_LOG_TOOLS:
        return None
    return {
        "type":  "agent_event",
        "agent": aid,
        "ts":    _now_hms(),
        "kind":  "tool_result",
        "title": f"✓ {name} ({m.group('dur')}s, {int(m.group('chars')):,} chars)",
        "live":  True,
    }


_register_pattern(
    r"agent\.tool_executor:\s+tool\s+(?P<name>\S+)\s+completed\s+\((?P<dur>[\d.]+)s,\s+(?P<chars>\d+)\s+chars\)",
    _tool_completed_builder,
)


def _tool_returned_error_builder(m, aid: str) -> dict:
    from hermes_console.services.slack_trace import trace_incident, _session_from_log_line
    name = m.group("name")
    trace_incident(aid, "tool_error", name, session_id=_session_from_log_line(m.string))
    return {
        "type":  "agent_event",
        "agent": aid,
        "ts":    _now_hms(),
        "kind":  "tool_error",
        "title": f"✗ {name} returned error",
        "live":  True,
    }


_register_pattern(
    r"agent\.tool_executor:\s+tool\s+(?P<name>\S+)\s+returned error",
    _tool_returned_error_builder,
)


def _api_call_builder(m, aid: str) -> dict:
    from hermes_console.services.usage import _estimate_openrouter_cost, _credit_api_call
    from hermes_console.services.prompt_trace import _trace_add, _trace_usage
    in_t  = int(m.group("in_t"))
    out_t = int(m.group("out_t"))
    cache_hits, cache_reads = 0, 0
    cmatch = _re.search(r"cache=(\d+)/(\d+)", m.string)
    if cmatch:
        cache_hits  = int(cmatch.group(1))
        cache_reads = int(cmatch.group(2))
    session_match = _re.search(r"\[(?P<session>\d{8}_\d{6}_[A-Za-z0-9]+)\]", m.string)
    session_id    = session_match.group("session") if session_match else ""
    latency = float(m.group("lat"))
    model   = m.group("model")
    cost    = _estimate_openrouter_cost(model, in_t, out_t, cache_hits)
    _credit_api_call(aid, in_t, out_t, cache_hits, cache_reads,
                     model=model, provider="openrouter",
                     latency_s=latency, session_id=session_id, call_no=m.group("n"))
    _trace_add(aid, "model.call", f"{model} · {in_t:,} in / {out_t:,} out · {latency}s")
    _trace_usage(aid, in_t, out_t, cost.get("estimated_cost_usd"))
    return {
        "type":  "agent_event",
        "agent": aid,
        "ts":    _now_hms(),
        "kind":  "tool_call",
        "title": f"🧠 model #{m.group('n')} · {m.group('lat')}s · {in_t:,}↑ {out_t:,}↓",
        "full":  (f"model: {model}\nlatency: {latency}s\nin/out tokens: {in_t:,} / {out_t:,}\n"
                  + (f"cache: {cache_hits:,}/{cache_reads:,} hit ratio "
                     f"({(100 * cache_hits // cache_reads) if cache_reads else 0}%)" if cache_reads else "")),
        "live":  True,
    }


_register_pattern(
    r"agent\.conversation_loop:\s+API call #(?P<n>\d+):\s+model=(?P<model>\S+)\s+provider=\S+\s+in=(?P<in_t>\d+)\s+out=(?P<out_t>\d+)\s+total=\d+\s+latency=(?P<lat>[\d.]+)s",
    _api_call_builder,
)


# ── Health issue patterns ──────────────────────────────────────────────────────

def _rate_limit_builder(m, aid: str) -> dict:
    from hermes_console.services.health import _raise_issue
    _raise_issue(f"rate_limit:{aid}", "warning", aid,
                 f"{aid}: rate limited (429)", "Provider is throttling requests — will auto-retry")
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": f"⚠️ Rate limited (429) — {aid}", "live": True}

_register_pattern(r"(?i)(429|rate.limit|too many requests)", _rate_limit_builder)


def _key_limit_builder(m, aid: str) -> dict:
    from hermes_console.services.health import _raise_issue
    _raise_issue("openrouter_no_credits", "critical", aid,
                 "OpenRouter API key limit reached",
                 "Key spending cap exceeded — raise the key limit in OpenRouter")
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": "🔑 OpenRouter key limit reached", "live": True}

_register_pattern(r"(?i)(Key limit exceeded|weekly limit\)|HTTP 403: Key limit exceeded)", _key_limit_builder)


def _auth_error_builder(m, aid: str) -> dict:
    from hermes_console.services.health import _raise_issue
    _raise_issue(f"auth_error:{aid}", "critical", aid,
                 f"{aid}: auth failure (401/403)", "API key rejected — check OPENROUTER_API_KEY in .env")
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": f"🔐 Auth error (401/403) — {aid}", "live": True}

_register_pattern(r"(?i)(HTTP\s*(?:401|403)\b|authentication.fail|api.key.invalid|invalid.api.key)", _auth_error_builder)


def _max_tokens_budget_builder(m, aid: str) -> dict:
    from hermes_console.services.health import _raise_issue
    _raise_issue(f"max_tokens_budget:{aid}", "warning", aid,
                 f"{aid}: OpenRouter max_tokens too high for key budget",
                 "Lower max_tokens or raise the key spending limit")
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": f"📉 max_tokens exceeds key budget — {aid}", "live": True}

_register_pattern(r"(?i)(requires more credits, or fewer max_tokens|can only afford \d+)", _max_tokens_budget_builder)


def _no_credits_builder(m, aid: str) -> dict:
    from hermes_console.services.health import _raise_issue
    _raise_issue("openrouter_no_credits", "critical", None,
                 "OpenRouter out of credits",
                 "insufficient_credits error from API — top up at openrouter.ai/credits")
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": "💳 Out of credits — OpenRouter", "live": True}

_register_pattern(r"(?i)(insufficient.credits|out.of.credits|credit.limit.exceeded|HTTP 402(?!.*max_tokens))", _no_credits_builder)


def _image_gen_fail_builder(m, aid: str) -> dict:
    from hermes_console.services.health import _raise_issue
    _raise_issue(f"image_gen_fail:{aid}", "warning", aid,
                 f"{aid}: image generation failed",
                 "Check image_gen provider/model config and API key")
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": f"🎨 Image generation failed — {aid}", "live": True}

_register_pattern(
    r"(?i)(image.gen(?:eration)?.fail|no.image.data|OpenRouter response contained no image|Gemini image generation failed)",
    _image_gen_fail_builder,
)


def _network_error_builder(m, aid: str) -> dict:
    from hermes_console.services.health import _raise_issue
    _raise_issue(f"network_error:{aid}", "warning", aid,
                 f"{aid}: network/timeout error", "Tool or API call timed out — may auto-retry")
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": f"🌐 Network error / timeout — {aid}", "live": True}

_register_pattern(
    r"(?i)(connection.timed.out|read.timeout|ConnectTimeout|network.error.*httpx|RemoteDisconnected)",
    _network_error_builder,
)


def _missing_key_builder(m, aid: str):
    from hermes_console.services.health import _raise_issue
    key_name = m.group("key") if "key" in m.groupdict() else "API key"
    _raise_issue(f"missing_key:{key_name}", "critical", aid,
                 f"Missing API key: {key_name}",
                 f"Set {key_name} in {HERMES_DIR}/.env and restart")
    return None

_register_pattern(r"(?P<key>(?:OPENROUTER|FAL|GEMINI|GOOGLE|OPENAI)_API_KEY)\s+not set", _missing_key_builder)


def _model_overload_builder(m, aid: str) -> dict:
    from hermes_console.services.health import _raise_issue
    _raise_issue(f"model_overload:{aid}", "warning", aid,
                 f"{aid}: model overloaded (503/529)",
                 "Provider is overloaded — Hermes will retry with backoff")
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": f"⏳ Model overloaded (503/529) — {aid}", "live": True}

_register_pattern(r"(?i)(HTTP 503|HTTP 529|model.overload|service.unavailable|overloaded)", _model_overload_builder)


def _context_limit_builder(m, aid: str) -> dict:
    from hermes_console.services.health import _raise_issue
    _raise_issue(f"context_limit:{aid}", "warning", aid,
                 f"{aid}: context limit hit",
                 "Session context is full — compression or reset needed")
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": f"📏 Context limit hit — {aid}", "live": True}

_register_pattern(r"(?i)(context.length.exceeded|context.window.full|maximum.context|token.limit.exceeded)", _context_limit_builder)


def _model_error_builder(m, aid: str) -> dict:
    from hermes_console.services.slack_trace import trace_incident
    session_id = m.group("session")
    trace_incident(aid, "model error", "finish_reason=error", session_id=session_id)
    return {"type": "agent_event", "agent": aid, "ts": _now_hms(), "kind": "tool_error",
            "title": f"🚨 model error (finish_reason=error) — {aid}", "live": True}

_register_pattern(
    r"agent\.conversation_loop:\s+Turn ended:.*finish_reason=error.*session=(?P<session>\S+)",
    _model_error_builder,
)


def _empty_response_retry_builder(m, aid: str):
    from hermes_console.services.slack_trace import trace_incident, _session_from_log_line
    if m.group("n") != m.group("max"):
        return None
    session_id = _session_from_log_line(m.string)
    trace_incident(aid, "empty response", "retries exhausted", session_id=session_id, severity="warn")
    return None

_register_pattern(
    r"agent\.conversation_loop:\s+Empty response \(no content or reasoning\) — retry (?P<n>\d+)/(?P<max>\d+)",
    _empty_response_retry_builder,
)


def _thinking_only_exhausted_builder(m, aid: str):
    from hermes_console.services.slack_trace import trace_incident, _session_from_log_line
    if m.group("n") != m.group("max"):
        return None
    session_id = _session_from_log_line(m.string)
    trace_incident(aid, "empty response", "thinking-only retries exhausted",
                   session_id=session_id, severity="warn")
    return None

_register_pattern(
    r"agent\.conversation_loop:\s+Thinking-only response \(no visible content\) — prefilling to continue \((?P<n>\d+)/(?P<max>\d+)\)",
    _thinking_only_exhausted_builder,
)


def _api_retries_exhausted_builder(m, aid: str):
    from hermes_console.services.slack_trace import trace_incident, _session_from_log_line
    session_id = _session_from_log_line(m.string)
    trace_incident(aid, "model error", "API retries exhausted", session_id=session_id)
    return None

_register_pattern(r"agent\.conversation_loop:\s+All API retries exhausted", _api_retries_exhausted_builder)


def _slack_delivery_failed_builder(m, aid: str):
    from hermes_console.services.slack_trace import trace_incident
    detail = m.group("detail") if "detail" in m.groupdict() else m.group(0)[:120]
    trace_incident("monitor", "slack delivery failed", detail[:120])
    return None

_register_pattern(
    r"(?i)(?:gateway\.platforms\.slack|gateway\.run).*(?:send.*failed|SlackApiError|channel_not_found|chat\.postMessage.*failed)",
    _slack_delivery_failed_builder,
)


# ── Public parse entry point ───────────────────────────────────────────────────

def parse_live_log_line(source: str, line: str) -> dict | None:
    """Translate a single log-tail line into a feed event.

    *source* is ``"gateway:<aid>"`` or ``"agent:<aid>"``.
    Returns ``None`` for lines we don't surface.
    """
    if ":" not in source:
        return None
    _, aid = source.split(":", 1)
    if aid not in AGENT_IDS:
        return None
    lower_line = line.lower()
    if "gateway.run:" in lower_line and (
        "no user allowlists configured" in lower_line
        or "unauthorized users will be denied" in lower_line
    ):
        return None
    for rx, builder in LIVE_LOG_PATTERNS:
        m = rx.search(line)
        if m:
            try:
                return builder(m, aid)
            except Exception:
                return None
    return None
