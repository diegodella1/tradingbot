const $ = (selector) => document.querySelector(selector);

const fmtUsd = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(value);
};

const fmtNum = (value, digits = 2) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(digits);
};

const fmtPct = (value, digits = 1) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(digits)}%`;
};

function renderKpis(kpis) {
  const wallet = kpis.paper_wallet || {};
  const pnl = Number(wallet.net_pnl_usdc ?? kpis.paper_pnl_usdc ?? 0);
  const winRate = kpis.win_rate;
  const roi = wallet.initial_cash_usdc ? pnl / Number(wallet.initial_cash_usdc) : kpis.paper_roi;
  $("#kpiRoi").textContent = fmtPct(roi);
  $("#kpiRoi").className = `font-data-lg text-2xl mt-2 ${pnl >= 0 ? "text-profit-emerald" : "text-loss-rose"}`;
  $("#kpiPnl").textContent = `${fmtUsd(pnl)} net equity PnL, ${fmtUsd(wallet.fees_paid_usdc || 0)} fees`;
  $("#kpiEdge").textContent = fmtPct(winRate);
  $("#kpiEdge").className = `font-data-lg text-2xl mt-2 ${Number(winRate || 0) >= 0.51 ? "text-profit-emerald" : "text-warning-amber"}`;
  $("#kpiConfidence").textContent = `${kpis.settled_trades || 0} settled, ${kpis.pending_settlement || 0} pending`;
  $("#kpiKelly").textContent = kpis.profit_factor === null || kpis.profit_factor === undefined ? "--" : fmtNum(kpis.profit_factor, 2);
  $("#kpiEntries").textContent = `${kpis.wins || 0} wins / ${kpis.losses || 0} losses`;
  $("#kpiDecisions").textContent = kpis.decision_count || 0;
  $("#kpiOrders").textContent = `${kpis.total_orders || 0} orders, ${kpis.total_fills || 0} fills`;
}

function renderTimeframes(rows) {
  $("#timeframeComparison").innerHTML = (rows || []).map((row) => {
    const edge = Number(row.avg_edge || 0);
    const edgePct = Math.max(0, Math.min(100, 50 + edge * 500));
    const cls = edge >= 0 ? "text-profit-emerald" : "text-loss-rose";
    const bar = edge >= 0 ? "bg-profit-emerald" : "bg-loss-rose";
    const settled = Number(row.settled || 0);
    const wr = row.win_rate;
    return `
      <div class="bg-surface-variant/30 rounded-lg p-4 border border-white/5">
        <h3 class="text-sm text-on-surface-variant mb-4 uppercase tracking-wider font-semibold">${row.market_type} Strategy</h3>
        <div class="space-y-4">
          <div>
            <div class="flex justify-between text-xs mb-1">
              <span class="text-on-surface">${settled > 0 ? "Settled Win Rate" : "Avg Decision Edge"}</span>
              <span class="${settled > 0 ? "text-profit-emerald" : cls} font-data-md">${settled > 0 ? fmtPct(wr) : `${fmtNum(edge * 100, 2)}c`}</span>
            </div>
            <div class="w-full bg-surface-container-high rounded-full h-1.5">
              <div class="${settled > 0 ? "bg-profit-emerald" : bar} h-1.5 rounded-full" style="width: ${settled > 0 ? Number(wr || 0) * 100 : edgePct}%"></div>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-4 pt-2">
            <div>
              <div class="text-xs text-on-surface-variant mb-1">Settled</div>
              <div class="font-data-md text-on-surface">${row.settled || 0}</div>
            </div>
            <div>
              <div class="text-xs text-on-surface-variant mb-1">W / L</div>
              <div class="font-data-md text-on-surface">${row.wins || 0} / ${row.losses || 0}</div>
            </div>
            <div>
              <div class="text-xs text-on-surface-variant mb-1">Realized</div>
              <div class="font-data-md ${(row.realized_pnl || 0) >= 0 ? "text-profit-emerald" : "text-loss-rose"}">${fmtUsd(row.realized_pnl)}</div>
            </div>
            <div>
              <div class="text-xs text-on-surface-variant mb-1">Decisions</div>
              <div class="font-data-md text-on-surface">${row.decisions || 0}</div>
            </div>
          </div>
        </div>
      </div>
    `;
  }).join("");
}

function renderReasons(reasons) {
  if (!reasons?.length) {
    $("#reasonRadar").innerHTML = `<p class="text-on-surface-variant text-sm">No decision reasons yet.</p>`;
    return;
  }
  const max = Math.max(...reasons.map((item) => item.count || 0), 1);
  $("#reasonRadar").innerHTML = reasons.slice(0, 6).map((item) => `
    <div>
      <div class="flex justify-between text-xs mb-1">
        <span class="text-on-surface truncate pr-2">${item.reason || "unknown"}</span>
        <span class="font-data text-outline">${item.count}</span>
      </div>
      <div class="w-full bg-surface-container-high rounded-full h-1.5">
        <div class="bg-btc-blue h-1.5 rounded-full" style="width: ${(item.count / max) * 100}%"></div>
      </div>
    </div>
  `).join("");
}

function renderHourly(hourly) {
  const max = Math.max(...(hourly || []).map((item) => Math.abs(item.avg_edge || 0)), 0.01);
  $("#hourlyBars").innerHTML = (hourly || []).map((item) => {
    const edge = Number(item.avg_edge || 0);
    const height = Math.max(4, Math.abs(edge) / max * 100);
    const cls = edge >= 0 ? "bg-profit-emerald hover:bg-profit-emerald/80" : "bg-loss-rose hover:bg-loss-rose/80";
    return `<div title="${String(item.hour).padStart(2, "0")}:00 ${fmtNum(edge * 100, 2)}c" class="w-full ${cls} transition-colors rounded-t-sm" style="height: ${height}%"></div>`;
  }).join("");
}

function renderHeatmap(outcomes) {
  const cells = [...(outcomes || [])].reverse().slice(-32);
  if (!cells.length) {
    $("#decisionHeatmap").innerHTML = `<div class="col-span-8 text-sm text-on-surface-variant">No paper positions yet.</div>`;
    return;
  }
  $("#decisionHeatmap").innerHTML = cells.map((position) => {
    const realized = Number(position.realized_pnl_usdc || 0);
    const unrealized = Number(position.unrealized_pnl_usdc || 0);
    const pnl = position.status === "OPEN" ? unrealized : realized;
    const magnitude = Math.min(0.95, Math.max(0.25, Math.abs(pnl) / Math.max(1, Number(position.cost_usdc || 1))));
    const color = position.status === "WON"
      ? `rgba(52, 211, 153, ${magnitude})`
      : position.status === "LOST"
        ? `rgba(251, 113, 133, ${magnitude})`
        : position.status === "EXPIRED_UNKNOWN"
          ? "rgba(245, 158, 11, 0.65)"
          : "rgba(0, 163, 255, 0.45)";
    const label = `${position.market_type || ""} ${position.side || ""} ${position.status || ""} PnL ${fmtUsd(pnl)} fees ${fmtUsd(position.fee_usdc || 0)}`;
    return `<div title="${label}" class="aspect-square rounded-sm border border-white/5" style="background:${color}"></div>`;
  }).join("");
}

function renderPositions(positions) {
  if (!positions?.length) {
    $("#positionsTable").innerHTML = `<tr><td class="px-6 py-4 text-on-surface-variant" colspan="6">No paper positions yet.</td></tr>`;
    return;
  }
  $("#positionsTable").innerHTML = positions.map((pos) => {
    const pnl = Number(pos.unrealized_pnl_usdc || pos.realized_pnl_usdc || 0);
    const pending = pos.status === "EXPIRED_UNKNOWN";
    const cls = pnl >= 0 ? "text-profit-emerald" : "text-loss-rose";
    return `
      <tr class="hover:bg-white/5 transition-colors">
        <td class="px-6 py-4 font-data-md text-xs">${pos.updated_at || "--"}</td>
        <td class="px-6 py-4">
          <span class="px-2 py-0.5 rounded text-[10px] bg-btc-blue/20 text-btc-blue border border-btc-blue/30 font-data-md">${pos.market_type || "--"}</span>
          <span class="ml-2 text-on-surface-variant">${pos.question || ""}</span>
        </td>
        <td class="px-6 py-4 font-data-md">${pos.side || "--"}</td>
        <td class="px-6 py-4 font-data-md">${fmtNum(pos.avg_price, 3)}</td>
        <td class="px-6 py-4 text-on-surface-variant">${pending ? "PENDING RESULT" : (pos.status || "--")}</td>
        <td class="px-6 py-4 font-data-md text-right ${pending ? "text-warning-amber" : cls}">${pending ? "pending" : fmtUsd(pnl)}</td>
      </tr>
    `;
  }).join("");
}

const ANALYTICS_REFRESH_MS = 15000;
const ANALYTICS_MAX_BACKOFF_MS = 60000;
const ANALYTICS_TIMEOUT_MS = 20000;
let analyticsTimer;
let analyticsInFlight = false;
let analyticsFailures = 0;

function scheduleAnalytics(delay = ANALYTICS_REFRESH_MS) {
  clearTimeout(analyticsTimer);
  if (!document.hidden) analyticsTimer = setTimeout(refreshAnalytics, delay);
}

async function refreshAnalytics() {
  if (analyticsInFlight || document.hidden) return;
  analyticsInFlight = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), ANALYTICS_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/analytics?ts=${Date.now()}`, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    analyticsFailures = 0;
    $("#analyticsUpdated").textContent = `SQLite paper metrics updated ${data.generated_at}`;
    const topbar = $("#topbarUpdated");
    if (topbar) topbar.textContent = `Updated ${data.generated_at}`;
    renderKpis(data.kpis || {});
    renderTimeframes(data.timeframe || []);
    renderReasons(data.reasons || []);
    renderHourly(data.hourly || []);
    renderHeatmap(data.outcomes || []);
    renderPositions(data.positions || []);
  } catch (error) {
    analyticsFailures += 1;
    const message = error.name === "AbortError" ? "request timed out" : error.message;
    $("#analyticsUpdated").textContent = `analytics error: ${message}`;
  } finally {
    clearTimeout(timeout);
    analyticsInFlight = false;
    const delay = Math.min(ANALYTICS_REFRESH_MS * (2 ** analyticsFailures), ANALYTICS_MAX_BACKOFF_MS);
    scheduleAnalytics(delay);
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(analyticsTimer);
  } else {
    refreshAnalytics();
  }
});

refreshAnalytics();
