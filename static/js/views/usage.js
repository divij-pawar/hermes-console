/**
 * views/usage.js — Token / cost usage panel.
 */

import { escHtml, fmtNum, fmtMoney, fmtShortTime } from "../util/format.js";
import { metaFor } from "../state/store.js";
import { scheduleRender, skipIfUnchanged } from "../util/renderQueue.js";

export function renderUsage(data) {
  if (skipIfUnchanged("usage", data)) return;
  scheduleRender("usage", () => _paintUsage(data));
}

function _paintUsage(data) {
  const active    = data.active    || {};
  const history   = data.history   || [];
  const ork       = data.openrouter || {};
  const providers = data.providers || {};
  const openrouter = providers.openrouter || {};
  const xUsage    = providers.x     || {};
  const tavily    = providers.tavily || {};

  const creditEl = document.getElementById("usage-credit");
  if (creditEl) {
    if (ork && typeof ork.usage === "number" && typeof ork.limit === "number") {
      const remaining = ork.limit - ork.usage;
      creditEl.textContent = `$${remaining.toFixed(2)} left`;
      creditEl.title = `Used $${ork.usage.toFixed(2)} of $${ork.limit.toFixed(2)} on OpenRouter`;
    } else if (ork.error) {
      creditEl.textContent = "—";
      creditEl.title = `OpenRouter: ${ork.error}`;
    } else {
      creditEl.textContent = "—";
    }
  }

  renderProviderUsageCards(openrouter, xUsage, tavily);

  const activeEl = document.getElementById("usage-active");
  if (activeEl) {
    const ids = Object.keys(active).sort();
    if (!ids.length) {
      activeEl.innerHTML = `<div class="usage-empty">No prompts tracked yet</div>`;
    } else {
      activeEl.innerHTML = ids.map(aid => {
        const a    = active[aid];
        const meta = metaFor(aid);
        const cacheRatio = a.cache_reads
          ? Math.round(100 * a.cache_hits / a.cache_reads)
          : null;
        return `
          <div class="usage-row" style="--agent-color:${meta.color}">
            <div class="usage-row-head">
              <span class="usage-agent">${meta.emoji} ${escHtml(meta.label)}</span>
              <span class="usage-calls">${a.calls} call${a.calls === 1 ? "" : "s"} · ${fmtMoney(a.estimated_cost_usd || 0)}</span>
            </div>
            <div class="usage-msg" title="${escHtml(a.msg || "")}">${escHtml(a.msg || "")}</div>
            <div class="usage-counts">
              <span title="input tokens">↑ ${fmtNum(a.input)}</span>
              <span title="output tokens">↓ ${fmtNum(a.output)}</span>
              ${cacheRatio !== null ? `<span title="cache hit ratio">cache ${cacheRatio}%</span>` : ""}
            </div>
          </div>
        `;
      }).join("");
    }
  }

  const histEl = document.getElementById("usage-history");
  if (histEl) {
    const items = [...history].reverse().slice(0, 10);
    if (!items.length) {
      histEl.innerHTML = `<div class="usage-empty">(empty)</div>`;
    } else {
      histEl.innerHTML = items.map(h => {
        const meta = metaFor(h.agent);
        return `
          <div class="usage-hist-row">
            <span class="usage-hist-tag">${meta.emoji}</span>
            <span class="usage-hist-msg">${escHtml((h.msg || "").slice(0, 60))}</span>
            <span class="usage-hist-counts">↑${fmtNum(h.input)} ↓${fmtNum(h.output)} · ${h.calls}c · ${fmtMoney(h.estimated_cost_usd || 0)}</span>
          </div>
        `;
      }).join("");
    }
  }
}

function renderProviderUsageCards(openrouter, xUsage, tavily) {
  const cardsEl = document.getElementById("usage-provider-cards");
  if (!cardsEl) return;
  const recentLine = (rows, fallback) => {
    const first = (rows || [])[0];
    if (!first) return fallback;
    const label = first.query || first.detail || first.model || first.kind || fallback;
    return `${fmtShortTime(first.ts)} · ${label}`.slice(0, 120);
  };
  const modelBits = (openrouter.models || []).slice(0, 2).map(m =>
    `<span>${escHtml(m.model || "model")}: ${fmtMoney(m.estimated_cost || 0)}</span>`
  ).join("");
  cardsEl.innerHTML = `
    <div class="usage-provider-card provider-openrouter">
      <div class="usage-provider-head"><span>OpenRouter</span><strong>${fmtMoney(openrouter.estimated_cost_today || 0)}</strong></div>
      <div class="usage-provider-sub">${fmtNum(openrouter.calls_today)} calls · ↑${fmtNum(openrouter.input_today)} ↓${fmtNum(openrouter.output_today)} · cache ${fmtNum(openrouter.cache_read_today)}</div>
      <div class="usage-provider-models">${modelBits || "<span>No model spend today</span>"}</div>
    </div>
    <div class="usage-provider-card provider-x">
      <div class="usage-provider-head"><span>X API</span><strong>${fmtNum(xUsage.calls_today)} calls</strong></div>
      <div class="usage-provider-sub">${fmtNum(xUsage.usage_units_today)} usage units · ${fmtNum(xUsage.failures_today)} failures</div>
      <div class="usage-provider-recent">${escHtml(recentLine(xUsage.recent, "No X searches recorded"))}</div>
    </div>
    <div class="usage-provider-card provider-tavily">
      <div class="usage-provider-head"><span>Tavily</span><strong>${fmtNum(tavily.calls_today)} calls</strong></div>
      <div class="usage-provider-sub">${fmtNum(tavily.usage_units_today)} usage units · ${fmtNum(tavily.failures_today)} failures</div>
      <div class="usage-provider-recent">${escHtml(recentLine(tavily.recent, "No Tavily calls recorded"))}</div>
    </div>
  `;
}
