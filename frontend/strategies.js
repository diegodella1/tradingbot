const $ = (selector) => document.querySelector(selector);

const fmtUsd = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(value));
};

const fmtNum = (value, digits = 2) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(digits);
};

const fmtPct = (value, digits = 1) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(digits)}%`;
};

const fmtCents = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(2)}c`;
};

const fmtAge = (hours) => {
  if (hours === null || hours === undefined || Number.isNaN(Number(hours))) return "never";
  const value = Number(hours);
  if (value < 1) return `${Math.round(value * 60)}m ago`;
  if (value < 48) return `${value.toFixed(1)}h ago`;
  return `${(value / 24).toFixed(1)}d ago`;
};

function byType(markets, type) {
  return (markets || []).find((market) => market.type === type || market.market_type === type);
}

function latestDecision(decisions, type) {
  return (decisions || []).find((decision) => decision.market_type === type) || null;
}

function sideClass(action) {
  if (String(action || "").includes("UP")) return "text-profit-emerald border-profit-emerald/30 bg-profit-emerald/20";
  if (String(action || "").includes("DOWN")) return "text-loss-rose border-loss-rose/30 bg-loss-rose/20";
  return "text-warning-amber border-warning-amber/30 bg-warning-amber/20";
}

function renderRuntime(data) {
  const safe = (data.safety || []).every((gate) => gate.ok);
  $("#connectionDot").className = `status-dot ${safe ? "dot-ok pulse-live" : "dot-warn"}`;
  $("#connectionLabel").textContent = safe ? "Paper connection" : "Safety warning";
  $("#navStatus").textContent = `${data.runtime?.status || "pending"} / ${data.strategy?.name || "unknown"}`;
  $("#updatedAt").textContent = `Updated ${data.generated_at}. Strategy: ${data.strategy?.name || "unknown"}. Browser config is read-only.`;
  $("#bankrollLabel").textContent = `${fmtUsd(data.config?.paper_bankroll_usdc)} paper`;
}

function renderStrategyCards(data) {
  const cards = ["5m", "15m"].map((type) => {
    const market = byType(data.markets, type);
    const decision = latestDecision(data.decisions, type);
    const signal = market?.signal || {};
    const action = decision?.action || signal.action || "HOLD";
    const active = Boolean(market);
    const confidence = Number(decision?.confidence ?? signal.confidence ?? 0);
    const edge = Number(decision?.edge ?? signal.edge ?? 0);
    const size = Number(decision?.recommended_size_usdc ?? signal.size_usdc ?? 0);
    const ask = action.includes("DOWN") ? market?.down_ask : market?.up_ask;
    const bid = action.includes("DOWN") ? market?.down_bid : market?.up_bid;
    const reason = decision?.reason || signal.reason || "waiting for paper loop";
    const badge = active ? (action === "HOLD" ? "Monitoring" : "Entry Candidate") : "No Market";
    const glow = action.includes("DOWN") ? "border-loss-rose/30" : action.includes("UP") ? "border-profit-emerald/30" : "border-outline-variant";

    return `
      <article class="strategy-card rounded-xl p-6 flex flex-col relative overflow-hidden ${glow}">
        <div class="absolute top-0 right-0 w-56 h-56 bg-btc-blue rounded-full mix-blend-screen blur-[90px] opacity-10 pointer-events-none"></div>
        <div class="flex justify-between items-start gap-4 mb-6 relative z-10">
          <div>
            <h3 class="font-headline text-3xl text-on-surface flex items-center gap-2">
              ${type.toUpperCase()} ${type === "5m" ? "Scalp" : "Trend"} Strategy
              <span class="bg-surface-container-highest px-2 py-0.5 rounded font-data text-sm ${active ? "text-profit-emerald" : "text-on-surface-variant"} border border-outline-variant">${badge}</span>
            </h3>
            <p class="text-sm text-on-surface-variant mt-1">${market?.question || "No active BTC Up/Down market in state yet."}</p>
          </div>
          <div class="px-3 py-1 rounded-full border text-xs uppercase tracking-wide font-bold ${sideClass(action)}">${action}</div>
        </div>

        <div class="space-y-5 flex-1 relative z-10">
          <div>
            <div class="flex justify-between mb-1">
              <label class="text-xs uppercase tracking-wide text-on-surface-variant font-bold">Signal Confidence</label>
              <span class="font-data text-primary">${fmtPct(confidence)}</span>
            </div>
            <input max="1" min="0" step="0.01" type="range" value="${Math.max(0, Math.min(1, confidence))}" disabled />
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div class="bg-surface-container-low p-4 rounded-lg border border-outline-variant/30">
              <label class="text-xs uppercase tracking-wide text-on-surface-variant mb-1 block">Edge</label>
              <div class="font-data text-lg ${edge >= 0 ? "text-profit-emerald" : "text-loss-rose"}">${fmtCents(edge)}</div>
            </div>
            <div class="bg-surface-container-low p-4 rounded-lg border border-outline-variant/30">
              <label class="text-xs uppercase tracking-wide text-on-surface-variant mb-1 block">Kelly Size</label>
              <div class="font-data text-lg text-on-surface">${fmtUsd(size)}</div>
            </div>
            <div class="bg-surface-container-low p-4 rounded-lg border border-outline-variant/30">
              <label class="text-xs uppercase tracking-wide text-on-surface-variant mb-1 block">Bid / Ask</label>
              <div class="font-data text-lg text-on-surface">${fmtNum(bid, 3)} / ${fmtNum(ask, 3)}</div>
            </div>
            <div class="bg-surface-container-low p-4 rounded-lg border border-outline-variant/30">
              <label class="text-xs uppercase tracking-wide text-on-surface-variant mb-1 block">Close</label>
              <div class="font-data text-lg text-on-surface">${market?.seconds_to_close ?? "--"}s</div>
            </div>
          </div>
        </div>

        <div class="mt-6 pt-5 border-t border-outline-variant/50 relative z-10">
          <h4 class="text-xs uppercase tracking-wide text-on-surface-variant mb-2 font-bold">Active Checks</h4>
          <div class="flex gap-2 flex-wrap">
            ${["Momentum", "Open Move", "Book Imbalance", "Spread", "Liquidity", "Hold to Resolution"].map((label) => `
              <span class="bg-surface-container px-2 py-1 rounded text-xs font-data text-on-surface flex items-center gap-1 border border-outline-variant/50">
                <span class="w-1.5 h-1.5 rounded-full ${active ? "bg-profit-emerald" : "bg-outline"}"></span>${label}
              </span>
            `).join("")}
          </div>
          <p class="text-sm text-on-surface-variant mt-4">${reason}</p>
        </div>
      </article>
    `;
  });

  $("#strategyCards").innerHTML = cards.join("");
}

function renderEngineParams(config) {
  const params = [
    ["Experimental Strategy", config.enable_experimental_strategy ? "Enabled" : "Disabled"],
    ["Min Edge", `${fmtNum(config.min_edge_cents, 2)}c`],
    ["Min Confidence", fmtPct(config.min_confidence)],
    ["Min Est. Probability", fmtPct(config.min_estimated_probability)],
    ["Entry Price Band", `${fmtNum(config.min_entry_price, 2)}-${fmtNum(config.max_entry_price, 2)}`],
    ["15m Min Entry", fmtNum(config.min_entry_price_15m, 2)],
    ["Core 15m Min Prob", fmtPct(config.min_probability_15m)],
    ["Scout 5m Min Prob", fmtPct(config.min_probability_5m)],
    ["Core 15m Net Edge", `${fmtNum(config.min_net_edge_15m_cents, 2)}c`],
    ["Scout 5m Net Edge", `${fmtNum(config.min_net_edge_5m_cents, 2)}c`],
    ["5m Scout", config.enable_5m_scout ? "enabled" : "disabled"],
    ["5m Scout Pause", `-${fmtUsd(config.disable_5m_after_recent_loss_usdc)} / ${config.recent_5m_loss_lookback}`],
    ["Danger Zone", `${fmtNum(config.danger_zone_min_price, 2)}-${fmtNum(config.danger_zone_max_price, 2)}`],
    ["Danger Min Prob", fmtPct(config.danger_zone_min_probability)],
    ["Danger Net Edge", `${fmtNum(config.danger_zone_min_net_edge_cents, 2)}c`],
    ["High Price Min Prob", fmtPct(config.high_price_min_probability)],
    ["Max Trades / Hour", config.max_trades_per_hour],
    ["Size Tiers", `${fmtUsd(config.size_tier_base_usdc)} / ${fmtUsd(config.size_tier_good_usdc)} / ${fmtUsd(config.size_tier_strong_usdc)} / ${fmtUsd(config.size_tier_max_usdc)}`],
    ["Drawdown Brake", `${config.drawdown_lookback_trades} trades / -${fmtUsd(config.drawdown_pause_loss_usdc)} / ${fmtPct(config.drawdown_size_multiplier)}`],
    ["15m Max Size", `${fmtPct(config.max_trade_pct_15m)} bankroll`],
    ["5m Max Size", `${fmtPct(config.max_trade_pct_5m)} bankroll`],
    ["Legacy Min Win Profit", fmtUsd(config.min_profit_if_win_usdc)],
    ["Fallback Min Net Edge", `${fmtNum(config.min_net_edge_cents, 2)}c`],
    ["Min Imbalance", fmtPct(config.min_book_imbalance)],
    ["5m Min Imbalance", fmtPct(config.min_book_imbalance_5m)],
    ["Kelly Multiplier", fmtNum(config.kelly_fraction_multiplier, 2)],
    ["Min Kelly Size", fmtUsd(config.min_kelly_size_usdc)],
    ["Token Max Exposure", fmtUsd(config.max_token_position_usdc)],
    ["Trade Size", fmtUsd(config.paper_trade_size_usdc)],
    ["Max Position", fmtUsd(config.max_position_usdc)],
    ["Daily Loss Stop", fmtUsd(config.max_daily_loss_usdc)],
    ["Max Open Markets", config.max_open_markets],
    ["Max Trades / Market", config.max_trades_per_market],
    ["Max Spread", `${fmtNum(config.max_spread_cents, 2)}c`],
    ["Min Liquidity", fmtUsd(config.min_orderbook_liquidity_usdc)],
    ["Min Seconds To Close", `${config.min_seconds_to_close}s`],
    ["Min Close 5m", config.min_seconds_to_close_5m == null ? "default" : `${config.min_seconds_to_close_5m}s`],
    ["Min Close 15m", config.min_seconds_to_close_15m == null ? "default" : `${config.min_seconds_to_close_15m}s`],
    ["Loop Interval", `${fmtNum(config.paper_loop_interval_seconds, 1)}s`]
  ];
  $("#engineParams").innerHTML = params.map(([label, value]) => `
    <div class="bg-surface-container-low p-4 rounded-lg border border-outline-variant/30">
      <label class="text-xs uppercase tracking-wide text-on-surface-variant mb-2 block">${label}</label>
      <div class="font-data text-lg text-on-surface">${value}</div>
    </div>
  `).join("");
}

function renderSafety(gates) {
  $("#safetyGates").innerHTML = (gates || []).map((gate) => `
    <div class="flex justify-between items-start gap-4">
      <div>
        <div class="text-sm text-on-surface">${gate.name}</div>
        <div class="text-xs text-on-surface-variant">${gate.detail || ""}</div>
      </div>
      <span class="font-data text-sm ${gate.ok ? "text-profit-emerald" : "text-warning-amber"}">${gate.ok ? "OK" : "BLOCK"}</span>
    </div>
  `).join("");
}

function renderTimeframes(rows) {
  $("#timeframeMetrics").innerHTML = (rows || []).map((row) => {
    const edge = Number(row.avg_edge || 0);
    return `
      <div class="bg-surface-container-low p-4 rounded-lg border border-outline-variant/30">
        <h4 class="text-sm uppercase tracking-wide text-on-surface-variant font-bold mb-3">${row.market_type} Market</h4>
        <div class="grid grid-cols-2 gap-3 font-data text-sm">
          <div><span class="text-outline block">Decisions</span><span class="text-on-surface">${row.decisions || 0}</span></div>
          <div><span class="text-outline block">Entries</span><span class="text-on-surface">${row.entries || 0}</span></div>
          <div><span class="text-outline block">Avg Edge</span><span class="${edge >= 0 ? "text-profit-emerald" : "text-loss-rose"}">${fmtCents(edge)}</span></div>
          <div><span class="text-outline block">Settled WR</span><span class="text-on-surface">${fmtPct(row.win_rate)}</span></div>
          <div><span class="text-outline block">W / L</span><span class="text-on-surface">${row.wins || 0} / ${row.losses || 0}</span></div>
          <div><span class="text-outline block">Realized</span><span class="${(row.realized_pnl || 0) >= 0 ? "text-profit-emerald" : "text-loss-rose"}">${fmtUsd(row.realized_pnl)}</span></div>
        </div>
      </div>
    `;
  }).join("");
}

function renderExecution(execution) {
  if (!$("#executionFunnel")) return;
  const stats = execution || {};
  const target = stats.target_entries_per_day || {};
  const btc = stats.feed_health?.btc || {};
  $("#executionSummary").textContent = `${stats.window || "24h"} · last entry ${fmtAge(stats.hours_since_latest_fill)} · target ${target.min || 2}–${target.max || 6}/day · BTC ${btc.source || "unknown"} ${fmtNum(btc.age_seconds, 1)}s · open ${fmtUsd(stats.open_exposure_usdc || 0)}`;
  const warnings = [];
  if (stats.stale_risk_warning) warnings.push("stale risk suspected");
  if (!btc.fresh) warnings.push("BTC feed stale");
  $("#executionWarning").textContent = warnings.join(" · ");
  const items = [
    ["Decisions", stats.decisions || 0],
    ["Signal Candidates", stats.signal_candidates || 0],
    ["Risk Approved", stats.risk_approved || 0],
    ["Paper Fills", stats.paper_fills || 0],
  ];
  $("#executionFunnel").innerHTML = items.map(([label, value]) => `
    <div class="bg-surface-container-low p-4 rounded-lg border border-outline-variant/30">
      <div class="text-xs uppercase tracking-wide text-outline">${label}</div>
      <div class="font-data text-2xl text-on-surface mt-1">${value}</div>
    </div>
  `).join("");
  const gateBlocks = (stats.top_gate_failures || []).map((block) => ({
    label: block.gate,
    count: block.count,
    detail: fmtPct(block.share),
  }));
  const riskBlocks = (stats.top_blocks || []).map((block) => ({
    label: `risk: ${block.reason}`,
    count: block.count,
    detail: "primary",
  }));
  $("#executionBlocks").innerHTML = [...gateBlocks, ...riskBlocks].slice(0, 12).map((block) => `
    <div class="bg-surface-container-low/60 p-3 rounded-lg border border-outline-variant/20">
      <div class="text-xs text-outline uppercase tracking-wide">${block.label}</div>
      <div class="font-data text-lg text-on-surface">${block.count}</div>
      <div class="text-xs text-on-surface-variant">${block.detail}</div>
    </div>
  `).join("");
}

function renderLearning(learning) {
  if (!$("#learningRecommendations")) return;
  const summary = learning?.summary || {};
  const recs = learning?.recommendations || [];
  const minimum = learning?.minimums?.total_settlements ?? 20;
  $("#learningSummary").textContent = `${summary.sample_size || 0} settlements · Net PnL ${fmtUsd(summary.pnl_usdc || 0)} · ROI ${fmtPct(summary.roi)} · minimum ${minimum} for global changes.`;
  if (!recs.length) {
    $("#learningRecommendations").innerHTML = `<div class="bg-surface-container-low p-4 rounded-lg border border-outline-variant/30 text-sm text-on-surface-variant">No active recommendations. Learning is disabled or no ledger data is available.</div>`;
  } else {
    $("#learningRecommendations").innerHTML = recs.map((rec) => {
      const status = rec.status || "observe";
      const tone = status === "ready_to_apply" ? "text-profit-emerald border-profit-emerald/30 bg-profit-emerald/10" : status === "candidate" ? "text-warning-amber border-warning-amber/30 bg-warning-amber/10" : "text-btc-blue border-btc-blue/30 bg-btc-blue/10";
      let suggested = {};
      try {
        suggested = rec.suggested_config_json ? JSON.parse(rec.suggested_config_json) : {};
      } catch {
        suggested = {};
      }
      const configText = Object.keys(suggested).length ? Object.entries(suggested).map(([key, value]) => `${key}=${value}`).join(" · ") : "No config change";
      return `
        <article class="bg-surface-container-low p-4 rounded-lg border border-outline-variant/30">
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="text-sm font-semibold text-on-surface">${rec.recommendation}</div>
              <div class="text-xs text-on-surface-variant mt-1">${rec.scope} · ${rec.metric}</div>
            </div>
            <span class="px-2 py-0.5 rounded border text-[10px] uppercase tracking-wide font-bold ${tone}">${status}</span>
          </div>
          <p class="text-sm text-on-surface-variant mt-3">${rec.rationale}</p>
          <div class="grid grid-cols-2 gap-2 mt-3 font-data text-xs">
            <div class="text-outline">Sample <span class="text-on-surface">${rec.sample_size}</span></div>
            <div class="text-outline">Confidence <span class="text-on-surface">${fmtPct(rec.confidence)}</span></div>
          </div>
          <div class="mt-3 text-xs font-data text-primary truncate">${configText}</div>
        </article>
      `;
    }).join("");
  }

  const versions = learning?.policy_versions || [];
  const versionRoot = $("#policyVersions");
  if (versionRoot) {
    versionRoot.innerHTML = versions.map((item) => {
      const metrics = item.metrics || {};
      const active = Boolean(item.is_active);
      return `
        <article class="bg-surface-container-low p-4 rounded-lg border ${active ? "border-profit-emerald/50" : "border-outline-variant/30"}">
          <div class="flex items-center justify-between gap-3">
            <strong class="text-sm text-on-surface truncate">${item.version}</strong>
            <span class="text-[10px] uppercase font-bold ${active ? "text-profit-emerald" : "text-on-surface-variant"}">${item.status}</span>
          </div>
          <div class="grid grid-cols-2 gap-2 mt-3 font-data text-xs">
            <div class="text-outline">Forward <span class="text-on-surface">${metrics.trades || 0}/200</span></div>
            <div class="text-outline">PnL <span class="${Number(metrics.pnl_usdc || 0) >= 0 ? "text-profit-emerald" : "text-loss-rose"}">${fmtUsd(metrics.pnl_usdc || 0)}</span></div>
            <div class="text-outline">ROI <span class="text-on-surface">${fmtPct(metrics.roi)}</span></div>
            <div class="text-outline">Drawdown <span class="text-on-surface">${fmtPct(metrics.max_drawdown_pct)}</span></div>
          </div>
          <p class="text-xs text-on-surface-variant mt-3">${item.gate_reason || "Historical cohort"}</p>
        </article>`;
    }).join("");
  }

  const refs = learning?.references || [];
  $("#learningReferences").innerHTML = refs.slice(0, 4).map((ref) => `
    <div class="bg-surface-container-low/60 p-3 rounded-lg border border-outline-variant/20">
      <div class="text-xs uppercase tracking-wide text-outline">${ref.title || "reference"}</div>
      <p class="text-xs text-on-surface-variant mt-1 line-clamp-2">${ref.snippet || ""}</p>
    </div>
  `).join("");
}

function renderDecisions(decisions) {
  $("#decisionCount").textContent = `${(decisions || []).length} recent decisions`;
  if (!decisions?.length) {
    $("#decisionsTable").innerHTML = `<tr><td class="px-6 py-4 text-on-surface-variant" colspan="7">No strategy decisions recorded yet.</td></tr>`;
    return;
  }
  $("#decisionsTable").innerHTML = decisions.map((decision) => {
    const edge = Number(decision.edge || 0);
    return `
      <tr class="hover:bg-surface-container-highest transition-colors">
        <td class="px-6 py-4 text-xs text-on-surface-variant">${decision.created_at || "--"}</td>
        <td class="px-6 py-4 text-on-surface">${decision.market_type || "--"}</td>
        <td class="px-6 py-4"><span class="px-2 py-0.5 rounded border text-xs uppercase ${sideClass(decision.action)}">${decision.action || "HOLD"}</span></td>
        <td class="px-6 py-4 text-right ${edge >= 0 ? "text-profit-emerald" : "text-loss-rose"}">${fmtCents(edge)}</td>
        <td class="px-6 py-4 text-right text-on-surface">${fmtNum(decision.kelly_fraction, 4)}</td>
        <td class="px-6 py-4 text-right text-on-surface">${fmtUsd(decision.recommended_size_usdc)}</td>
        <td class="px-6 py-4 text-on-surface-variant">${decision.reason || "--"}</td>
      </tr>
    `;
  }).join("");
}

async function refreshStrategies() {
  const response = await fetch(`/api/strategies?ts=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = await response.json();
  renderRuntime(data);
  renderStrategyCards(data);
  renderEngineParams(data.config || {});
  renderSafety(data.safety || []);
  renderTimeframes(data.timeframe || []);
  renderExecution(data.execution || {});
  renderLearning(data.learning || {});
  renderDecisions(data.decisions || []);
}

refreshStrategies().catch((error) => {
  $("#updatedAt").textContent = `strategies error: ${error.message}`;
});
setInterval(refreshStrategies, 5000);
